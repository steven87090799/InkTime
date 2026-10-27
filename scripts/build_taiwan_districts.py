"""Build bounded per-town geometry from the official NLSC open-data ZIP.

No photo coordinates are sent anywhere. Only public source data is consumed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import zipfile


def records(blob):
    count = struct.unpack_from('<I', blob, 4)[0]
    header, length = struct.unpack_from('<HH', blob, 8)
    fields = []
    offset = 32
    while blob[offset] != 13:
        field = blob[offset:offset + 32]
        fields.append((field[:11].split(b'\0')[0].decode('ascii'), field[16]))
        offset += 32
    result = []
    for index in range(count):
        row = blob[header + index * length:header + (index + 1) * length]
        offset = 1
        values = {}
        for key, size in fields:
            raw = row[offset:offset + size].strip()
            offset += size
            try:
                values[key] = raw.decode('utf-8')
            except UnicodeDecodeError:
                values[key] = raw.decode('big5')
        result.append(values)
    return result


def polygons(blob):
    offset = 100
    while offset < len(blob):
        _, words = struct.unpack_from('>II', blob, offset)
        body = blob[offset + 8:offset + 8 + words * 2]
        offset += 8 + words * 2
        if struct.unpack_from('<I', body)[0] not in {5, 15}:
            raise ValueError('Expected polygon geometry')
        count, points = struct.unpack_from('<II', body, 36)
        starts = list(struct.unpack_from('<' + 'I' * count, body, 44)) + [points]
        xy = struct.unpack_from('<' + 'dd' * points, body, 44 + 4 * count)
        rings = [[[round(xy[i * 2], 6), round(xy[i * 2 + 1], 6)]
                  for i in range(starts[n], starts[n + 1])] for n in range(count)]
        yield list(struct.unpack_from('<dddd', body, 4)), rings


def build(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    index = []
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED) as out:
        # The official Sanhe overlay takes priority over the base town layer.
        for layer in ['Town_Majia_Sanhe', 'TOWN_MOI_1120317']:
            names = records(src.read(layer + '.dbf'))
            shapes = list(polygons(src.read(layer + '.shp')))
            if len(names) != len(shapes):
                raise ValueError('Attribute/geometry count mismatch')
            for position, (attributes, (bounds, rings)) in enumerate(zip(names, shapes, strict=True)):
                code = attributes['TOWNCODE']
                if not code.isdigit():
                    raise ValueError('Invalid town code')
                name = f'{layer}-{position}-{code}.json'
                out.writestr(name, json.dumps(rings, separators=(',', ':')))
                index.append({'file': name, 'bounds': bounds, 'district': attributes['TOWNNAME'],
                              'city': attributes['COUNTYNAME'], 'code': code})
        metadata = {'version': 'nlsc-1120317-sanhe', 'features': index,
                    'source': 'https://maps.nlsc.gov.tw/pro/download.jsp',
                    'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                    'license': 'https://data.gov.tw/license', 'crs': 'TWD97[2020] geographic degrees'}
        out.writestr('index.json', json.dumps(metadata, ensure_ascii=False, separators=(',', ':')))
    print(json.dumps({'features': len(index), 'bytes': target.stat().st_size,
                      'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('target', type=Path)
    args = parser.parse_args()
    build(args.source, args.target)
