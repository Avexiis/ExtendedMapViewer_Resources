import os
import sys
import struct
import argparse
import warnings
from math import ceil

from PIL import Image, ImageFile, Image as PILImage
from PIL import Image as _PIL_Image
from PIL import Image as _Image

Image.MAX_IMAGE_PIXELS = None
from PIL import Image as __Image
from PIL import Image
warnings.simplefilter('ignore', Image.DecompressionBombWarning)
ImageFile.LOAD_TRUNCATED_IMAGES = True

TILE_PX_DEFAULT = 256
MAGIC = b'ATLSv1\x00\x00'
VERSION = 2
NUM_LAYERS = 4

def parse_args():
    p = argparse.ArgumentParser(
        description="Pack runelite world-plane.png files into a tiled, lossless 3D .atlas container."
    )
    p.add_argument(
        "--tile",
        type=int,
        default=TILE_PX_DEFAULT,
        help="Tile size (default 256)"
    )
    p.add_argument(
        "--lods",
        default="1,2,4",
        help="List of LOD subsamples to include (ex. 4 or 2,4 or 1,2,4)"
    )
    return p.parse_args()

def iter_tiles(img, tile_px):
    tiles_x = ceil(img.width / tile_px)
    tiles_y = ceil(img.height / tile_px)
    for ty in range(tiles_y):
        y0 = ty * tile_px
        y1 = min(y0 + tile_px, img.height)
        for tx in range(tiles_x):
            x0 = tx * tile_px
            x1 = min(x0 + tile_px, img.width)
            yield tx, ty, (x0, y0, x1, y1)

def encode_png_bytes(tile_img):
    from io import BytesIO
    b = BytesIO()
    if tile_img.mode != "RGBA":
        tile_img = tile_img.convert("RGBA")
    tile_img.save(b, format="PNG", optimize=True, compress_level=9)
    data = b.getvalue()
    return data, tile_img.width, tile_img.height

def build_downsampled(img, subsample):
    if subsample == 1:
        return img
    w = img.width // subsample
    h = img.height // subsample
    return img.resize((w, h), resample=Image.BOX)

def pack_atlas_3d(src_png_paths, out_atlas_path, tile_px, lods):
    if len(src_png_paths) != NUM_LAYERS:
        raise ValueError(f"Expected {NUM_LAYERS} src PNGs, got {len(src_png_paths)}")

    base_images = []
    width = None
    height = None

    for z, path in enumerate(src_png_paths):
        img = Image.open(path)
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        if width is None:
            width = img.width
            height = img.height
        else:
            if img.width != width or img.height != height:
                raise ValueError(
                    f"All source PNGs must match in size. "
                    f"z0 is {width}x{height}, but {path} is {img.width}x{img.height}"
                )

        base_images.append(img)

    with open(out_atlas_path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<I", VERSION))
        f.write(struct.pack("<I", width))
        f.write(struct.pack("<I", height))
        f.write(struct.pack("<I", tile_px))
        f.write(struct.pack("<I", len(lods)))
        for lod in lods:
            f.write(struct.pack("<I", lod))
        tiles_x_full = ceil(width / tile_px)
        tiles_y_full = ceil(height / tile_px)
        f.write(struct.pack("<I", tiles_x_full))
        f.write(struct.pack("<I", tiles_y_full))
        f.write(struct.pack("<I", NUM_LAYERS))
        f.write(struct.pack("<Q", 0))
        f.write(struct.pack("<Q", 0))

        header_end = f.tell()

        index_entries = []
        tile_data_blobs = []

        for subsample in lods:
            downsampled_layers = []
            for z in range(NUM_LAYERS):
                img = base_images[z]
                if subsample == 1:
                    base = img
                else:
                    base = build_downsampled(img, subsample)
                downsampled_layers.append(base)

            for z, base in enumerate(downsampled_layers):
                for tx, ty, (x0, y0, x1, y1) in iter_tiles(base, tile_px):
                    tile = base.crop((x0, y0, x1, y1))
                    png_bytes, w, h = encode_png_bytes(tile)
                    index_entries.append([subsample, z, tx, ty, w, h, None, len(png_bytes)])
                    tile_data_blobs.append(png_bytes)

                if subsample != 1:
                    try:
                        base.close()
                    except Exception:
                        pass

        index_offset = f.tell()

        # Component bytes:
        #   uByte32: lod, z, tx, ty, w, h, length
        #   uByte64: rel_off
        entry_size = 36
        index_size = len(index_entries) * entry_size
        data_offset = index_offset + index_size

        cursor = 0
        for i, entry in enumerate(index_entries):
            entry[6] = cursor
            cursor += len(tile_data_blobs[i])

        for lod, z, tx, ty, w, h, rel_off, length in index_entries:
            f.write(struct.pack("<I", lod))
            f.write(struct.pack("<I", z))
            f.write(struct.pack("<I", tx))
            f.write(struct.pack("<I", ty))
            f.write(struct.pack("<I", w))
            f.write(struct.pack("<I", h))
            f.write(struct.pack("<Q", rel_off))
            f.write(struct.pack("<I", length))

        assert f.tell() == data_offset

        for blob in tile_data_blobs:
            f.write(blob)

        # Header layout:
        #  MAGIC (8)
        #  VERSION (4)
        #  width (4)
        #  height (4)
        #  tile_px (4)
        #  num_lods (4)
        #  lods (4 * len(lods))
        #  tiles_x_full (4)
        #  tiles_y_full (4)
        #  num_layers (4)
        #  index_offset (8)
        #  data_offset (8)
        f.seek(header_end - 16)
        f.write(struct.pack("<Q", index_offset))
        f.write(struct.pack("<Q", data_offset))

    for img in base_images:
        try:
            img.close()
        except Exception:
            pass

    print("Wrote:", out_atlas_path)
    print(
        "Source size:", width, "x", height,
        "| Layers (Z):", NUM_LAYERS,
        "| Tile:", tile_px,
        "| LODs:", lods,
        "| Entries:", len(index_entries)
    )

def main():
    args = parse_args()
    try:
        lods = [int(x.strip()) for x in args.lods.split(",") if x.strip()]
        for v in lods:
            if v not in (1, 2, 4):
                raise ValueError("LOD values must be subset of 1,2,4")
        if not lods:
            raise ValueError("At least one LOD required")
    except Exception as e:
        print("Invalid --lods:", e)
        sys.exit(2)

    script_dir = os.path.dirname(os.path.abspath(__file__))

    src_png_paths = [
        os.path.join(script_dir, f"world_plane{i}.png")
        for i in range(NUM_LAYERS)
    ]
    out_atlas_path = os.path.join(script_dir, "atlas_bg.atlas")

    for path in src_png_paths:
        if not os.path.isfile(path):
            print(f"Missing required input: {path}")
            sys.exit(1)

    pack_atlas_3d(src_png_paths, out_atlas_path, tile_px=args.tile, lods=lods)

if __name__ == "__main__":
    main()
