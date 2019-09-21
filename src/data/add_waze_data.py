import argparse
from . import util
import os
import json
import geojson
from collections import defaultdict
from .record import Record

BASE_DIR = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.abspath(__file__))))


def get_linestring(value):
    """
    Turns a waze linestring into a geojson linestring
    Args:
        value - the waze dict for this line
    Returns:
        geojson linestring with properties
    """

    line = value['line']
    coords = [(x['x'], x['y']) for x in line]
    return geojson.Feature(
        geometry=geojson.LineString(coords),
        properties=value
    )


def get_features(waze_info, properties, num_snapshots):
    """
    Given a dict with keys of segment id, and val a list of waze jams
    (for now, just jams), the properties of a road segment, and the
    total number of snapshots we're looking at, update the road segment's
    properties to include features
    Args:
        waze_info - dict
        properties - dict
        num_snapshots
    Returns
        properties
    """
    # Waze feature list
    # jam_percent - percentage of snapshots that have a jam on this segment
    if properties['segment_id'] in waze_info:

        # only count one jam per snapshot on a road
        num_jams = len(set([x['properties']['snapshotId']
                            for x in waze_info[properties['segment_id']]]))
        # The average jam level across all jam instances
        avg_level_when_jammed = round(sum(
            [x['properties']['level']
             for x in waze_info[properties['segment_id']]]
        )/len(waze_info[properties['segment_id']]))
        avg_speed = round(sum(
            [x['properties']['speed']
             for x in waze_info[properties['segment_id']]]
        )/len(waze_info[properties['segment_id']]))
    else:
        num_jams = 0
        avg_speed = 0
        avg_level_when_jammed = 0

    # Turn into number between 0 and 100
    properties.update(jam_percent=100*num_jams/num_snapshots)
    properties.update(jam=1 if num_jams else 0)
    properties.update(avg_jam_speed=avg_speed)
    properties.update(avg_jam_level=avg_level_when_jammed)
    
    return properties


def add_alerts(items, road_segments):

    roads, roads_index = util.index_segments(
        road_segments, geojson=True, segment=True)

    # We'll want to consider making these point-based features at some point
    items = [Record(x) for x in items
             if x['eventType'] == 'alert']

    util.find_nearest(
        items, roads, roads_index, 30, type_record=True)

    # Turn records into a dict
    items_dict = defaultdict(dict)
    for item in items:
        if item.properties['type'] not in items_dict[item.near_id]:
            items_dict[item.near_id][item.properties['type']] = 0
        items_dict[item.near_id][item.properties['type']] += 1

    for road in road_segments:
        properties = road.properties
        if properties['id'] in items_dict:
            for key in items_dict[properties['id']].keys():
                properties['alert_' + key] = items_dict[properties['id']][key]

        road.properties = properties

    return road_segments


def map_segments(datadir, filename, forceupdate=False):
    """
    Map a set of waze segment info (jams) onto segments drawn from
    openstreetmap: the osm_elements.geojson file
    Args:
        datadir - directory where the city's data is found
        filename - the filename of the json aggregated waze file
    Returns:
        nothing - just updates osm_elements.geojson and writes
            a jams.geojson with the segments that have jams
    """
    items = json.load(open(filename))

    # Get the total number of snapshots in the waze data
    num_snapshots = max([x['snapshotId'] for x in items])

    osm_file = os.path.join(
        datadir,
        'processed',
        'maps',
        'osm_elements.geojson'
    )
    road_segments, inters = util.get_roads_and_inters(osm_file)
    if 'jam' in road_segments[0].properties and not forceupdate:
        print("Already processed waze data")
        return

    # Add jam and alert information
    road_segments, roads_with_jams = add_jams(
        items, road_segments, inters, num_snapshots)
    road_segments = add_alerts(items, road_segments)

    # Convert into format that util.prepare_geojson is expecting
    geojson_roads = []
    for road in road_segments:
        geojson_roads.append({
            'geometry': {
                'coordinates': [x for x in road.geometry.coords],
                'type': 'LineString'
            },
            'properties': road.properties
        })
    # Convert this back to geojson from shapely point
    inters = [{
        'geometry': {
            'type': 'Point',
            'coordinates': [x['geometry'].x, x['geometry'].y],
        },
        'properties': x['properties']
    } for x in inters]

    results = util.prepare_geojson(geojson_roads + inters)

    with open(osm_file, 'w') as outfile:
        geojson.dump(results, outfile)

    jam_results = util.prepare_geojson(roads_with_jams)

    with open(os.path.join(
            datadir,
            'processed',
            'maps',
            'jams.geojson'), 'w') as outfile:
        geojson.dump(jam_results, outfile)


def add_jams(items, road_segments, inters, num_snapshots):

    # Only look at jams for now
    items = [get_linestring(x) for x in items
             if x['eventType'] == 'jam']
    items = util.reproject_records(items)

    # Get roads_and_inters returns elements that have shapely geometry
    # In order to output the unchanged points back out at the end,
    # Need to convert to geojson
    # This is something that should be addressed
    inters = [{'properties': x['properties'], 'geometry': {
        'type': 'Point',
        'coordinates': [x['geometry'].x, x['geometry'].y]
    }} for x in inters]
    
    roads, roads_index = util.index_segments(
        road_segments, geojson=True, segment=True)

    road_buffers = []

    for road in roads:
        road_buffers.append(road.geometry.buffer(3))

    print("read in {} road segments".format(len(roads)))

    waze_info = defaultdict(list)
    count = 0

    for item in items:
        count += 1

        if item['properties']['eventType'] == 'jam':
            for idx in roads_index.intersection(item['geometry'].bounds):
                segment = roads[idx]
                buff = road_buffers[idx]

                # But if the roads share a name,
                # increase buffer size, in case of a median segment
                # Waze does not appear to specify which direction
                if 'street' in item['properties'] and segment.properties['name'] and \
                   item['properties']['street'].split()[0] == segment.properties['name'].split()[0]:
                    buff = segment.geometry.buffer(10)
                overlap = buff.intersection(item['geometry'])

                if not overlap.length or \
                   (overlap.length < 20 and segment.geometry.length > 20):
                    # Skip segments with no overlap
                    # or very short overlaps
                    continue
                waze_info[segment.properties['segment_id']].append(item)
    # Add waze features
    roads_with_jams = []

    for road in road_segments:
        properties = get_features(
            waze_info,
            road.properties,
            num_snapshots
        )
        road.properties = properties
        if properties['segment_id'] in waze_info:
            roads_with_jams.append({
                    'geometry': {
                        'coordinates': [x for x in road.geometry.coords],
                        'type': 'LineString'
                    },
                    'properties': properties
            })
    return road_segments, roads_with_jams


def make_map(filename, datadir):
    """
    Turns a json file into a geojson file of linestrings
    Used mainly for visualization/debugging
    It is not a simplified set of linestrings, but rather a
    linestring for each jam instance (even if multiple jam
    instances are on the same segment)
    Args:
        filename - input json file
        datadir - directory to write the waze.geojson file out
    """
    items = json.load(open(filename))
    geojson_items = []
    for item in items:
        if item['eventType'] == 'jam':
            geojson_items.append(get_linestring(item))
    with open(os.path.join(datadir, 'waze.geojson'), 'w') as outfile:
        geojson.dump(geojson.FeatureCollection(geojson_items), outfile)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument("-d", "--datadir", type=str,
                        help="data directory")
    parser.add_argument('--forceupdate', action='store_true',
                        help='Whether to force update of the waze data')

    args = parser.parse_args()

    infile = os.path.join(args.datadir, 'standardized', 'waze.json')
#    make_map(infile, os.path.join(args.datadir, 'processed', 'maps'))
    map_segments(args.datadir, infile, forceupdate=args.forceupdate)
