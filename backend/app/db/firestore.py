from google.cloud.firestore import Client
from google.cloud.exceptions import NotFound
from app.core.config import settings

db = Client(project=settings.GOOGLE_CLOUD_PROJECT)

def get_document(collection: str, document_id: str) -> dict:
    try:
        doc_ref = db.collection(collection).document(document_id)
        doc = doc_ref.get()
        return doc.to_dict() if doc.exists else None
    except NotFound:
        return None

def create_document(collection: str, data: dict) -> str:
    doc_ref = db.collection(collection).add(data)
    return doc_ref[1].id

def update_document(collection: str, document_id: str, data: dict) -> bool:
    try:
        doc_ref = db.collection(collection).document(document_id)
        doc_ref.update(data)
        return True
    except NotFound:
        return False

def delete_document(collection: str, document_id: str) -> bool:
    try:
        doc_ref = db.collection(collection).document(document_id)
        doc_ref.delete()
        return True
    except NotFound:
        return False

# HUMAN ASSISTANCE NEEDED
# The following function might need additional error handling and optimization for production use
def query_documents(collection: str, filters: dict) -> list:
    query = db.collection(collection)
    for field, value in filters.items():
        query = query.where(field, '==', value)
    docs = query.stream()
    return [doc.to_dict() for doc in docs]