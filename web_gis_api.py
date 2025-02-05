#!/usr/bin/env python3
import os
import sys
import json
import subprocess
import tempfile
from osgeo import gdal, osr
from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import uuid
import base64
from google.cloud import storage
from google.oauth2 import service_account
import io
import datetime

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# GCS Configuration
BUCKET_NAME = os.getenv('BUCKET_NAME', 'web-gis-2198')

# Initialize GCS client
def get_storage_client():
    """Get storage client based on environment"""
    if os.getenv('GAE_ENV', '').startswith('standard'):
        # Running on App Engine, use default credentials
        return storage.Client()
    else:
        # Local development, use service account key
        credentials_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', 'key.json')
        if os.path.exists(credentials_path):
            credentials = service_account.Credentials.from_service_account_file(credentials_path)
            return storage.Client(credentials=credentials)
        else:
            raise Exception(f"Service account key not found at {credentials_path}")

storage_client = get_storage_client()
bucket = storage_client.bucket(BUCKET_NAME)

# Create a transparent PNG for missing tiles
TRANSPARENT_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAACgAAAAoCAQAAAC1+jfqAAAAGElEQVRIx2NgoBvoKGKAgP///4Y8AwMDAwMDAwMAAAD/7+bw12hhOwAAAABJRU5ErkJggg=="
)

def upload_to_gcs(local_path, gcs_path):
    """Upload a file to Google Cloud Storage."""
    blob = bucket.blob(gcs_path)
    blob.upload_from_filename(local_path)
    print(f"Uploaded {local_path} to gs://{BUCKET_NAME}/{gcs_path}")

def upload_directory_to_gcs(local_dir, folder_id):
    """Upload an entire directory to Google Cloud Storage under a unique folder."""
    for root, dirs, files in os.walk(local_dir):
        for file in files:
            local_path = os.path.join(root, file)
            # Create GCS path directly under the bucket with folder_id
            relative_path = os.path.relpath(local_path, local_dir)
            gcs_path = os.path.join(folder_id, relative_path)
            upload_to_gcs(local_path, gcs_path)

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
        
        # Create a unique ID and temporary directory
        unique_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temp_dir:
            # Save the uploaded image temporarily
            temp_image_path = os.path.join(temp_dir, 'source_image' + os.path.splitext(image_file.filename)[1])
            image_file.save(temp_image_path)

            # Create intermediate filenames in temp directory
            vrt_path = os.path.join(temp_dir, "temp.vrt")
            warped_path = os.path.join(temp_dir, "warped.tif")
            tiles_output_dir = os.path.join(temp_dir, "tiles")

            # Step 1: Create the VRT file with GCPs
            create_vrt_with_gcps(temp_image_path, points_data, vrt_path)

            # Step 2: Warp (reproject) the VRT to a georeferenced raster
            warp_image(vrt_path, warped_path)

            # Step 3: Generate XYZ tiles from the warped image
            generate_tiles(warped_path, tiles_output_dir)

            # Step 4: Upload all generated files to GCS directly under the unique folder
            upload_directory_to_gcs(tiles_output_dir, unique_id)
            
            # Also upload the source image and intermediate files for reference
            upload_to_gcs(temp_image_path, f'{unique_id}/source_image{os.path.splitext(image_file.filename)[1]}')
            upload_to_gcs(vrt_path, f'{unique_id}/temp.vrt')
            upload_to_gcs(warped_path, f'{unique_id}/warped.tif')

        return jsonify({
            'status': 'success',
            'output_directory': unique_id
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/<folder_id>/tiles/<z>/<x>/<y>.png')
def get_xyz_tile(folder_id, z, x, y):
    """
    Serve a tile from Google Cloud Storage
    Convert from XYZ coordinates to TMS coordinates (flip y axis)
    """
    try:
        # Convert from XYZ to TMS coordinates
        zoom = int(z)
        tms_y = (2**zoom - 1) - int(y)  # Flip Y coordinate
        
        # Construct GCS path directly with folder_id
        gcs_path = f'{folder_id}/tiles/{z}/{x}/{tms_y}.png'
        
        print(f"🗺️ XYZ request: z={z}, x={x}, y={y}")
        print(f"🔄 Converting to TMS: z={z}, x={x}, y={tms_y}")
        print(f"🔍 Looking for: gs://{BUCKET_NAME}/{gcs_path}")
        
        # Try to get the blob
        blob = bucket.blob(gcs_path)
        
        if blob.exists():
            # Download the tile to memory and serve it
            content = blob.download_as_bytes()
            return Response(
                response=content,
                mimetype='image/png',
                headers={'Access-Control-Allow-Origin': '*'}
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

@app.route('/health')
def health_check():
    """
    Health check endpoint that verifies:
    1. API is running
    2. GCS connectivity
    """
    try:
        # Check GCS connectivity by listing a single blob
        next(bucket.list_blobs(max_results=1), None)
        
        return jsonify({
            'status': 'healthy',
            'gcs_bucket': BUCKET_NAME,
            'timestamp': str(datetime.datetime.now())
        })
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e),
            'timestamp': str(datetime.datetime.now())
        }), 500

if __name__ == "__main__":
    # This is used when running locally only. When deploying to Google App
    # Engine, a webserver process such as Gunicorn will serve the app. This
    # can be configured by adding an `entrypoint` to app.yaml.
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
