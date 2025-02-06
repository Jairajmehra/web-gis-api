#!/usr/bin/env python3
import os
import sys
import json
import subprocess
from osgeo import gdal, osr
from flask import Flask, request, jsonify, Response, send_from_directory
import uuid
import base64
import datetime
from flask_cors import CORS

app = Flask(__name__)
#CORS(app)  # Enable CORS for all routes
cors = CORS(app, resources={r"/*": {"origins": "*"}})
# Create tiles directory if it doesn't exist
TILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tiles')
os.makedirs(TILES_DIR, exist_ok=True)

# Create a transparent PNG for missing tiles
TRANSPARENT_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAACgAAAAoCAQAAAC1+jfqAAAAGElEQVRIx2NgoBvoKGKAgP///4Y8AwMDAwMDAwMAAAD/7+bw12hhOwAAAABJRU5ErkJggg=="
)

def create_vrt_with_gcps(image_path, points_data, vrt_path):
    """
    Create a GDAL VRT file from an input image by embedding GCPs (ground control points)
    from points data.
    """
    # Open the source image.
    ds = gdal.Open(image_path)
    if ds is None:
        raise Exception("Could not open image: " + image_path)

    gcps = []
    for pt in points_data["points"]:
        img_x = float(pt["image"]["x"])
        img_y = float(pt["image"]["y"])
        lat   = float(pt["map"]["lat"])
        lng   = float(pt["map"]["lng"])
        gcp = gdal.GCP(lng, lat, 0, img_x, img_y)
        gcps.append(gcp)

    # Define the spatial reference for the GCPs.
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)  # The map points are in EPSG:4326 (lat/lng)

    # Set the GCPs on the dataset.
    ds.SetGCPs(gcps, srs.ExportToWkt())

    # Create a VRT from the dataset.
    vrt_ds = gdal.Translate(vrt_path, ds, format="VRT")
    vrt_ds = None
    ds = None
    print("VRT created at:", vrt_path)

def warp_image(vrt_path, warped_path):
    """
    Warp (reproject) the image defined in the VRT into a georeferenced raster.
    """
    warp_cmd = [
        "gdalwarp",
        "-tps",                  # use Thin Plate Spline interpolation
        "-r", "bilinear",        # resampling method
        "-s_srs", "EPSG:4326",   # source SRS (GCPs are in lat/lng)
        "-t_srs", "EPSG:3857",   # target SRS (Web Mercator)
        vrt_path,
        warped_path
    ]
    print("Running gdalwarp:")
    print(" ".join(warp_cmd))
    subprocess.check_call(warp_cmd)
    print("Warped image created at:", warped_path)

def generate_tiles(warped_path, output_tiles_dir):
    """
    Generate XYZ tiles using gdal2tiles.py.
    """
    tiles_cmd = [
        "gdal2tiles.py",
        "-z", "9-16",          # zoom levels 9 to 16
        warped_path,
        output_tiles_dir
    ]
    print("Running gdal2tiles.py:")
    print(" ".join(tiles_cmd))
    subprocess.check_call(tiles_cmd)
    print("Tiles generated in:", output_tiles_dir)

def convert_to_rgba(input_path, output_path):
    """
    Convert an input image to an RGBA GeoTIFF in a robust way.
    
    For single-band images:
      - If a color table is present (indexed/paletted image), expand to RGBA.
      - Otherwise (grayscale), expand to RGB.
    
    For multi-band images:
      - If there are exactly 3 bands, add an alpha channel.
      - If there are 4 or more bands, assume the image is already RGBA (or similar) and simply copy it.
    """
    try:
        # Open the input dataset
        ds = gdal.Open(input_path)
        if ds is None:
            raise Exception("Unable to open input file: " + input_path)
        band_count = ds.RasterCount

        # For a single-band image, decide based on the presence of a color table
        if band_count == 1:
            band = ds.GetRasterBand(1)
            if band.GetRasterColorTable() is not None:
                # Indexed image: use -expand rgba to convert from the palette
                vrt_temp = output_path + '.vrt'
                cmd1 = [
                    "gdal_translate",
                    "-of", "VRT",
                    "-expand", "rgba",
                    input_path,
                    vrt_temp
                ]
                print("Converting indexed image to VRT with RGBA:", " ".join(cmd1))
                subprocess.check_call(cmd1)

                cmd2 = [
                    "gdal_translate",
                    "-of", "GTiff",
                    "-co", "ALPHA=YES",
                    vrt_temp,
                    output_path
                ]
                print("Converting VRT to GTiff:", " ".join(cmd2))
                subprocess.check_call(cmd2)
                os.remove(vrt_temp)
            else:
                # Grayscale image: expand to RGB (three bands)
                cmd = [
                    "gdal_translate",
                    "-of", "GTiff",
                    "-expand", "rgb",
                    input_path,
                    output_path
                ]
                print("Converting grayscale image to RGB:", " ".join(cmd))
                subprocess.check_call(cmd)

        elif band_count == 3:
            # For 3-band images, add an alpha channel using GDAL's ALPHA creation option
            cmd = [
                "gdal_translate",
                "-of", "GTiff",
                "-co", "ALPHA=YES",
                input_path,
                output_path
            ]
            print("Adding alpha channel to 3-band image:", " ".join(cmd))
            subprocess.check_call(cmd)

        elif band_count >= 4:
            # For images that already have 4 or more bands, assume they are RGBA (or similar)
            # Simply copy the file to the output (or add the ALPHA creation option if needed)
            cmd = [
                "gdal_translate",
                "-of", "GTiff",
                input_path,
                output_path
            ]
            print("Copying multi-band image:", " ".join(cmd))
            subprocess.check_call(cmd)
        else:
            raise Exception("Unexpected number of bands: " + str(band_count))

        print("RGBA conversion complete at:", output_path)
    except Exception as e:
        print(f"Error during conversion: {str(e)}")
        raise


@app.route('/generate_xyz_tiles', methods=['POST'])
def generate_tiles_endpoint():
    try:
        # Check if image file is present in request
        if 'image' not in request.files:
            return jsonify({'error': 'No image file provided'}), 400
        
        # Get the image file
        image_file = request.files['image']
        
        # Get the points data
        if 'points' not in request.form:
            return jsonify({'error': 'No points data provided'}), 400
            
        points_data = json.loads(request.form['points'])
        
        # Create a unique ID
        unique_id = str(uuid.uuid4())
        folder_path = os.path.join(TILES_DIR, unique_id)
        os.makedirs(folder_path, exist_ok=True)

        # Save the uploaded image
        image_path = os.path.join(folder_path, 'source_image' + os.path.splitext(image_file.filename)[1])
        image_file.save(image_path)

        # Create paths for intermediate files
        vrt_path = os.path.join(folder_path, "temp.vrt")
        warped_path = os.path.join(folder_path, "warped.tif")
        rgba_path = os.path.join(folder_path, "rgba.tif")  # New RGBA file
        tiles_output_dir = os.path.join(folder_path, "tiles")

        # Step 1: Create the VRT file with GCPs
        create_vrt_with_gcps(image_path, points_data, vrt_path)

        # Step 2: Warp (reproject) the VRT to a georeferenced raster
        warp_image(vrt_path, warped_path)

        # Step 3: Convert to RGBA
        convert_to_rgba(warped_path, rgba_path)

        # Step 4: Generate XYZ tiles from the RGBA image
        generate_tiles(rgba_path, tiles_output_dir)

        return jsonify({
            'status': 'success',
            'output_directory': unique_id
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/<folder_id>/tiles/<z>/<x>/<y>.png')
def get_xyz_tile(folder_id, z, x, y):
    """
    Serve a tile from local filesystem
    Convert from XYZ coordinates to TMS coordinates (flip y axis)
    """
    try:
        # Convert from XYZ to TMS coordinates
        zoom = int(z)
        tms_y = (2**zoom - 1) - int(y)  # Flip Y coordinate
        
        # Construct local filesystem path
        tile_path = os.path.join('tiles', folder_id, 'tiles', z, x, f'{tms_y}.png')
        
        print(f"🗺️ XYZ request: z={z}, x={x}, y={y}")
        print(f"🔄 Converting to TMS: z={z}, x={x}, y={tms_y}")
        print(f"🔍 Looking for: {tile_path}")
        
        # Check if tile exists
        if os.path.exists(tile_path):
            return send_from_directory(
                os.path.dirname(os.path.abspath(__file__)),
                tile_path,
                mimetype='image/png'
            )
        else:
            return Response(
                response=TRANSPARENT_PNG,
                mimetype='image/png',
                headers={'Access-Control-Allow-Origin': '*'}
            )
            
    except Exception as e:
        print(f"Error serving tile: {str(e)}")
        return Response(
            response=TRANSPARENT_PNG,
            mimetype='image/png',
            headers={'Access-Control-Allow-Origin': '*'}
        )

@app.route('/check_folder_id')
def check_folder_exists():
    """
    Check if a folder exists in the local tiles directory.
    Takes folder_id as a URL parameter.
    Returns true if folder exists, false otherwise.
    """
    try:
        # Get folder_id from URL parameters
        folder_id = request.args.get('folder_id')
        if not folder_id:
            return jsonify({
                'error': 'No folder_id provided',
                'exists': False
            }), 400
        
        # Check if folder exists in tiles directory
        folder_path = os.path.join(TILES_DIR, folder_id)
        exists = os.path.exists(folder_path)
        
        return jsonify({
            'folder_id': folder_id,
            'exists': exists
        })
        
    except Exception as e:
        return jsonify({
            'error': str(e),
            'exists': False
        }), 500

@app.route('/live')
def liveness_probe():
    """Simple liveness probe"""
    return jsonify({
        'status': 'live',
        'service': 'web-gis-api',
        'timestamp': str(datetime.datetime.now())
    })

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8085)), debug=False)
