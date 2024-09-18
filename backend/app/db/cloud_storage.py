from google.cloud.storage import Client
from app.core.config import settings

storage_client = Client(project=settings.GOOGLE_CLOUD_PROJECT)
bucket = storage_client.bucket(settings.GOOGLE_CLOUD_STORAGE_BUCKET)

def upload_file(file_content: bytes, destination_blob_name: str) -> str:
    blob = bucket.blob(destination_blob_name)
    blob.upload_from_string(file_content)
    blob.make_public()
    return blob.public_url

# HUMAN ASSISTANCE NEEDED
# The following function might need additional error handling or logging for production readiness
def delete_file(blob_name: str) -> bool:
    blob = bucket.blob(blob_name)
    if blob.exists():
        blob.delete()
        return True
    return False

def get_file_url(blob_name: str) -> str:
    blob = bucket.blob(blob_name)
    if blob.exists():
        return blob.public_url
    return None