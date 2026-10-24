#!/usr/bin/env python3
"""png_resize.py - tiny dependency-free PNG reader/scaler/writer.

The deb build needs hicolor icons at exactly 256x256 and 512x512.  Build
machines cannot be assumed to have ImageMagick, but every CI image that builds
this package has Python 3, so this file implements the few PNG operations the
build needs using only the standard library (``zlib``/``struct``):

  * read 8-bit/16-bit, non-interlaced PNG files (gray, RGB, palette,
    gray+alpha, RGBA, plus tRNS transparency);
  * resize with a box filter when downscaling (good for app icons) and
    bilinear interpolation when upscaling;
  * write a plain 8-bit RGBA PNG.

Usage:
    python3 png_resize.py --info path/to/icon.png
    python3 png_resize.py path/to/icon.png out-256.png 256 256

Exit codes: 0 success, 1 usage/I/O error, 3 unsupported PNG variant.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

CHANNELS = {
    0: 1,  # grayscale
    2: 3,  # RGB
    3: 1,  # palette
    4: 2,  # grayscale + alpha
    6: 4,  # RGBA
}


class PngError(Exception):
    """The input is not a PNG this tool can process."""


def _read_chunks(data):
    if not data.startswith(PNG_SIGNATURE):
        raise PngError("missing PNG signature")
    chunks = []
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(data):
        length, chunk_type = struct.unpack_from(">I4s", data, offset)
        offset += 8
        if offset + length + 4 > len(data):
            raise PngError("truncated %r chunk" % chunk_type)
        payload = data[offset : offset + length]
        offset += length + 4  # skip CRC (validated by decoders we care about)
        chunks.append((chunk_type, payload))
        if chunk_type == b"IEND":
            break
    if not chunks:
        raise PngError("no PNG chunks found")
    return chunks


def read_png_info(path):
    """Return (width, height, bit_depth, color_type, interlace)."""
    with open(path, "rb") as handle:
        data = handle.read()
    for chunk_type, payload in _read_chunks(data):
        if chunk_type == b"IHDR":
            if len(payload) < 13:
                raise PngError("truncated IHDR chunk")
            width, height, bit_depth, color_type, compression, filter_method, interlace = (
                struct.unpack(">IIBBBBB", payload[:13])
            )
            if compression != 0 or filter_method != 0:
                raise PngError("unsupported PNG compression/filter method")
            return width, height, bit_depth, color_type, interlace
    raise PngError("PNG has no IHDR chunk")


def _paeth(a, b, c):
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw, width, height, channels, bytes_per_sample):
    """Reverse the per-scanline PNG filters and return flat RGBA bytes."""
    stride = width * channels * bytes_per_sample
    bpp = channels * bytes_per_sample
    previous = bytearray(stride)
    rows = []
    offset = 0
    for _ in range(height):
        if offset >= len(raw):
            raise PngError("truncated image data")
        filter_type = raw[offset]
        offset += 1
        line = bytearray(raw[offset : offset + stride])
        if len(line) < stride:
            raise PngError("truncated scanline")
        offset += stride
        if filter_type == 0:
            pass
        elif filter_type == 1:
            for index in range(stride):
                left = line[index - bpp] if index >= bpp else 0
                line[index] = (line[index] + left) & 0xFF
        elif filter_type == 2:
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 0xFF
        elif filter_type == 3:
            for index in range(stride):
                left = line[index - bpp] if index >= bpp else 0
                up = previous[index]
                line[index] = (line[index] + ((left + up) >> 1)) & 0xFF
        elif filter_type == 4:
            for index in range(stride):
                left = line[index - bpp] if index >= bpp else 0
                up = previous[index]
                up_left = previous[index - bpp] if index >= bpp else 0
                line[index] = (line[index] + _paeth(left, up, up_left)) & 0xFF
        else:
            raise PngError("unknown PNG filter type %d" % filter_type)
        rows.append(line)
        previous = line
    return rows


def _to_rgba(rows, width, height, bit_depth, color_type, palette, transparency):
    """Convert unfiltered scanlines to a flat RGBA bytearray."""
    if bit_depth not in (8, 16):
        raise PngError(
            "unsupported bit depth %d (only 8 and 16 are supported)" % bit_depth
        )
    sample_bytes = bit_depth // 8
    channels = CHANNELS[color_type]
    rgba = bytearray(width * height * 4)
    if color_type in (0, 2, 4, 6):
        transparent_color = None
        if transparency is not None and color_type in (0, 2):
            count = 1 if color_type == 0 else 3
            if len(transparency) >= count * 2:
                values = struct.unpack(">" + "H" * count, transparency[: count * 2])
                transparent_color = tuple(value >> 8 for value in values)
        for y in range(height):
            row = rows[y]
            base = y * width * 4
            if bit_depth == 8:
                for x in range(width):
                    offset = x * channels
                    if color_type == 0:
                        gray = row[offset]
                        alpha = 255
                        if transparent_color is not None and gray == transparent_color[0]:
                            alpha = 0
                        rgba[base + x * 4 : base + x * 4 + 4] = bytes(
                            (gray, gray, gray, alpha)
                        )
                    elif color_type == 2:
                        red, green, blue = row[offset : offset + 3]
                        alpha = 255
                        if (
                            transparent_color is not None
                            and (red, green, blue) == transparent_color
                        ):
                            alpha = 0
                        rgba[base + x * 4 : base + x * 4 + 4] = bytes(
                            (red, green, blue, alpha)
                        )
                    elif color_type == 4:
                        gray, alpha = row[offset], row[offset + 1]
                        rgba[base + x * 4 : base + x * 4 + 4] = bytes(
                            (gray, gray, gray, alpha)
                        )
                    else:  # RGBA
                        rgba[base + x * 4 : base + x * 4 + 4] = row[offset : offset + 4]
            else:  # 16-bit samples -> keep the most significant byte
                for x in range(width):
                    offset = x * channels * 2
                    if color_type == 0:
                        gray = row[offset]
                        rgba[base + x * 4 : base + x * 4 + 4] = bytes(
                            (gray, gray, gray, 255)
                        )
                    elif color_type == 2:
                        rgba[base + x * 4 : base + x * 4 + 4] = bytes(
                            (row[offset], row[offset + 2], row[offset + 4], 255)
                        )
                    elif color_type == 4:
                        rgba[base + x * 4 : base + x * 4 + 4] = bytes(
                            (row[offset], row[offset], row[offset], row[offset + 2])
                        )
                    else:  # RGBA
                        rgba[base + x * 4 : base + x * 4 + 4] = bytes(
                            (row[offset], row[offset + 2], row[offset + 4], row[offset + 6])
                        )
        return rgba

    # Palette images.
    if not palette:
        raise PngError("palette PNG is missing a PLTE chunk")
    plte = bytearray(len(palette) // 3 * 4)
    for index in range(len(palette) // 3):
        plte[index * 4 : index * 4 + 3] = palette[index * 3 : index * 3 + 3]
        plte[index * 4 + 3] = 255
    if transparency is not None:
        for index, alpha in enumerate(transparency):
            if index * 4 + 3 < len(plte):
                plte[index * 4 + 3] = alpha
    for y in range(height):
        row = rows[y]
        base = y * width * 4
        for x in range(width):
            index = row[x]
            source = index * 4
            rgba[base + x * 4 : base + x * 4 + 4] = plte[source : source + 4]
    return rgba


def read_png_rgba(path):
    """Decode a PNG file to (width, height, flat RGBA bytearray)."""
    with open(path, "rb") as handle:
        data = handle.read()
    chunks = _read_chunks(data)
    ihdr = None
    palette = None
    transparency = None
    idat = bytearray()
    for chunk_type, payload in chunks:
        if chunk_type == b"IHDR":
            ihdr = payload
        elif chunk_type == b"PLTE":
            palette = payload
        elif chunk_type == b"tRNS":
            transparency = payload
        elif chunk_type == b"IDAT":
            idat.extend(payload)
    if ihdr is None or len(ihdr) < 13:
        raise PngError("PNG has no IHDR chunk")
    width, height, bit_depth, color_type, compression, filter_method, interlace = (
        struct.unpack(">IIBBBBB", ihdr[:13])
    )
    if compression != 0 or filter_method != 0:
        raise PngError("unsupported PNG compression/filter method")
    if interlace != 0:
        raise PngError("interlaced PNG files are not supported")
    if color_type not in CHANNELS:
        raise PngError("unsupported PNG color type %d" % color_type)
    if bit_depth not in (8, 16):
        raise PngError(
            "unsupported bit depth %d (only 8 and 16 are supported)" % bit_depth
        )
    if width <= 0 or height <= 0:
        raise PngError("PNG has invalid dimensions %dx%d" % (width, height))
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise PngError("cannot decompress PNG image data: %s" % exc)
    channels = CHANNELS[color_type]
    rows = _unfilter(raw, width, height, channels, bit_depth // 8)
    rgba = _to_rgba(rows, width, height, bit_depth, color_type, palette, transparency)
    return width, height, rgba


def resize_rgba(rgba, width, height, new_width, new_height):
    """Resize a flat RGBA bytearray; box filter down, bilinear up."""
    if new_width <= 0 or new_height <= 0:
        raise PngError("target dimensions must be positive")
    output = bytearray(new_width * new_height * 4)
    scale_x = width / float(new_width)
    scale_y = height / float(new_height)
    if scale_x >= 1.0 and scale_y >= 1.0:
        # Box filter: average the source rectangle covered by each target pixel.
        for y in range(new_height):
            y0 = int(y * scale_y)
            y1 = max(y0 + 1, min(height, int((y + 1) * scale_y)))
            for x in range(new_width):
                x0 = int(x * scale_x)
                x1 = max(x0 + 1, min(width, int((x + 1) * scale_x)))
                count = (x1 - x0) * (y1 - y0)
                red = green = blue = alpha_sum = opaque = 0
                for source_y in range(y0, y1):
                    row_base = source_y * width * 4
                    for source_x in range(x0, x1):
                        offset = row_base + source_x * 4
                        alpha = rgba[offset + 3]
                        alpha_sum += alpha
                        if alpha == 0:
                            continue
                        opaque += 1
                        red += rgba[offset]
                        green += rgba[offset + 1]
                        blue += rgba[offset + 2]
                target = (y * new_width + x) * 4
                if opaque > 0:
                    output[target] = min(255, red // opaque)
                    output[target + 1] = min(255, green // opaque)
                    output[target + 2] = min(255, blue // opaque)
                output[target + 3] = alpha_sum // count
        return output

    # Bilinear sampling for enlargement.
    for y in range(new_height):
        source_y = (y + 0.5) * scale_y - 0.5
        y0 = max(0, min(height - 1, int(source_y)))
        y1 = min(height - 1, y0 + 1)
        fy = max(0.0, min(1.0, source_y - y0))
        for x in range(new_width):
            source_x = (x + 0.5) * scale_x - 0.5
            x0 = max(0, min(width - 1, int(source_x)))
            x1 = min(width - 1, x0 + 1)
            fx = max(0.0, min(1.0, source_x - x0))
            target = (y * new_width + x) * 4
            for channel in range(4):
                top_left = rgba[(y0 * width + x0) * 4 + channel]
                top_right = rgba[(y0 * width + x1) * 4 + channel]
                bottom_left = rgba[(y1 * width + x0) * 4 + channel]
                bottom_right = rgba[(y1 * width + x1) * 4 + channel]
                top = top_left + (top_right - top_left) * fx
                bottom = bottom_left + (bottom_right - bottom_left) * fx
                output[target + channel] = int(top + (bottom - top) * fy + 0.5)
    return output


def _chunk(chunk_type, payload):
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def encode_png_rgba(rgba, width, height, level=9):
    """Encode a flat RGBA bytearray as a non-interlaced 8-bit PNG."""
    stride = width * 4
    filtered = bytearray()
    for y in range(height):
        filtered.append(0)  # filter type None
        filtered.extend(rgba[y * stride : (y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    compressed = zlib.compress(bytes(filtered), level)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", compressed)
        + _chunk(b"IEND", b"")
    )


def resize_file(source, destination, width, height):
    source_width, source_height, rgba = read_png_rgba(source)
    resized = resize_rgba(rgba, source_width, source_height, width, height)
    data = encode_png_rgba(resized, width, height)
    with open(destination, "wb") as handle:
        handle.write(data)
    return source_width, source_height, width, height, len(data)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="png_resize.py",
        description="Read, scale and write PNG icons with the Python standard library.",
    )
    parser.add_argument("source", help="input PNG file")
    parser.add_argument("destination", nargs="?", help="output PNG file")
    parser.add_argument("width", nargs="?", type=int, help="target width in pixels")
    parser.add_argument("height", nargs="?", type=int, help="target height in pixels")
    parser.add_argument(
        "--info",
        action="store_true",
        help="print width/height/bit-depth/color-type/interlace and exit",
    )
    args = parser.parse_args(argv)
    try:
        if args.info:
            width, height, bit_depth, color_type, interlace = read_png_info(args.source)
            print(
                "%dx%d bit_depth=%d color_type=%d interlace=%d"
                % (width, height, bit_depth, color_type, interlace)
            )
            return 0
        if args.destination is None or args.width is None or args.height is None:
            parser.error("destination, width and height are required unless --info is used")
        source_width, source_height, width, height, size = resize_file(
            args.source, args.destination, args.width, args.height
        )
        print(
            "resized %s (%dx%d) -> %s (%dx%d, %d bytes)"
            % (args.source, source_width, source_height, args.destination, width, height, size)
        )
        return 0
    except (OSError, PngError) as exc:
        print("png_resize.py: %s" % exc, file=sys.stderr)
        return 3 if isinstance(exc, PngError) else 1


if __name__ == "__main__":
    sys.exit(main())
