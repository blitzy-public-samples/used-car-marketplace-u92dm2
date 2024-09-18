import unittest
from fastapi.testclient import TestClient
from app.main import app
from app.models import User, Listing, Transaction, Message
from app.database import SessionLocal
from app.auth import create_access_token

class TestAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    # Test cases for authentication endpoints
    def test_register_user(self):
        response = self.client.post("/auth/register", json={
            "username": "testuser",
            "email": "testuser@example.com",
            "password": "testpassword"
        })
        self.assertEqual(response.status_code, 201)
        self.assertIn("id", response.json())

    def test_login_user(self):
        response = self.client.post("/auth/login", data={
            "username": "testuser",
            "password": "testpassword"
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("access_token", response.json())

    # Test cases for listing creation, retrieval, update, and deletion
    def test_create_listing(self):
        token = create_access_token({"sub": "testuser"})
        response = self.client.post("/listings/", json={
            "title": "Test Listing",
            "description": "This is a test listing",
            "price": 100.00,
            "category": "Electronics"
        }, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 201)
        self.assertIn("id", response.json())

    def test_get_listing(self):
        # Assume a listing with id 1 exists
        response = self.client.get("/listings/1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "Test Listing")

    def test_update_listing(self):
        token = create_access_token({"sub": "testuser"})
        response = self.client.put("/listings/1", json={
            "title": "Updated Test Listing",
            "description": "This is an updated test listing",
            "price": 150.00,
            "category": "Electronics"
        }, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "Updated Test Listing")

    def test_delete_listing(self):
        token = create_access_token({"sub": "testuser"})
        response = self.client.delete("/listings/1", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 204)

    # Test cases for transaction processing
    def test_create_transaction(self):
        token = create_access_token({"sub": "testuser"})
        response = self.client.post("/transactions/", json={
            "listing_id": 1,
            "buyer_id": 2,
            "amount": 100.00
        }, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 201)
        self.assertIn("id", response.json())

    def test_get_transaction(self):
        token = create_access_token({"sub": "testuser"})
        response = self.client.get("/transactions/1", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["amount"], 100.00)

    # Test cases for messaging functionality
    def test_send_message(self):
        token = create_access_token({"sub": "testuser"})
        response = self.client.post("/messages/", json={
            "recipient_id": 2,
            "content": "Hello, this is a test message"
        }, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 201)
        self.assertIn("id", response.json())

    def test_get_messages(self):
        token = create_access_token({"sub": "testuser"})
        response = self.client.get("/messages/", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)

    # Test cases for error handling and edge cases
    def test_invalid_login(self):
        response = self.client.post("/auth/login", data={
            "username": "nonexistentuser",
            "password": "wrongpassword"
        })
        self.assertEqual(response.status_code, 401)

    def test_create_listing_without_auth(self):
        response = self.client.post("/listings/", json={
            "title": "Unauthorized Listing",
            "description": "This should fail",
            "price": 100.00,
            "category": "Electronics"
        })
        self.assertEqual(response.status_code, 401)

    def test_get_nonexistent_listing(self):
        response = self.client.get("/listings/9999")
        self.assertEqual(response.status_code, 404)

    # HUMAN ASSISTANCE NEEDED
    # The following test cases might need to be adjusted based on the actual implementation of rate limiting and input validation
    def test_rate_limiting(self):
        # Implement test for rate limiting
        pass

    def test_input_validation(self):
        # Implement tests for various input validation scenarios
        pass

if __name__ == "__main__":
    unittest.main()