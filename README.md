# Used Car Marketplace

A comprehensive platform for buying and selling used cars, connecting sellers with potential buyers in a user-friendly and efficient manner.

## Features

- User registration and authentication
- Car listing creation and management
- Advanced search and filtering options
- Messaging system for buyer-seller communication
- User ratings and reviews: buyers rate sellers and sellers rate buyers on a 1 to 5 scale, with an optional written review, restricted to the two counterparties of a completed transaction
- Favorite listings and saved searches
- Admin panel for platform management

## Technologies Used

- Frontend: React.js
- Backend: Python with FastAPI
- Database: Google Cloud Firestore in native mode (the `users`, `listings`, `transactions`, `messages` and `ratings` collections)
- Authentication: JSON Web Tokens (JWT)
- State Management: Redux Toolkit
- Styling: Tailwind CSS (utility-first)
- Testing: pytest for the backend, Vitest with React Testing Library for the frontend
- Deployment: Docker and Kubernetes

## Getting Started

### Prerequisites

- Node.js (v14 or later)
- Python 3.9
- A Google Cloud project with Firestore in native mode, and the `gcloud` CLI
- Docker (optional, for containerized deployment)

### Installation

1. Clone the repository:
   ```
   git clone https://github.com/your-username/used-car-marketplace.git
   cd used-car-marketplace
   ```

2. Install dependencies:
   ```
   npm install
   ```

3. Set up environment variables:
   - Copy `backend/.env.example` to `backend/.env`
   - Update the variables in `backend/.env` with your specific configuration

4. Start the development server:
   ```
   npm run dev
   ```

5. Open your browser and navigate to `http://localhost:3000`

## Usage

1. Register an account or log in if you already have one
2. Browse car listings or create your own listing
3. Use the search and filter options to find specific cars
4. Contact sellers through the messaging system
5. Leave a review and rating for your counterparty once a transaction is completed: one rating per participant, from a verified account

## Ratings and Reviews

The marketplace runs a bidirectional peer reputation system: for a purchase they both took part in, the buyer rates the seller and the seller rates the buyer. A rating is an integer score from 1 to 5, plus an optional written review whose length the server bounds.

Two authorization gates govern every submission, and the server enforces both:

- The rater must be a verified user. Accounts carry an `is_verified` flag, and an unverified caller is refused with `403`. Verified here means account (email) verification only; the platform performs no identity or KYC verification, which is an explicitly excluded feature of the project.
- The rater and the rated party must be the two counterparties of the same transaction, and that transaction must be completed. A caller who is neither the buyer nor the seller of the cited transaction is refused with `403`, and a transaction that has not completed is refused with `409`.

Each participant may rate a given transaction once. A second submission is refused with `409`, and that constraint is enforced by the datastore rather than by an application check, so it holds even when two submissions arrive at the same moment.

Who is rated is decided by the server, not by the rater. The rated party and the direction of the rating (buyer to seller, or seller to buyer) are derived from the transaction record rather than read from the request, so a rating cannot be redirected at somebody else and nobody can rate themselves.

Publication is double-blind. A submitted rating stays unpublished, and is excluded from the recipient's average, until the counterparty submits theirs or the rating window elapses, so neither side can retaliate against the other's score. The length of that window is configurable.

Every user carries a `rating_average` and a `rating_count` computed from published ratings only. Both are shown on the user profile and beside the seller on a vehicle's detail page.

An administrator may withhold a review for a policy violation (abuse, personally identifying information, or profanity), with the reason recorded. A rating is never withheld for being low, and the average includes every published rating regardless of its score.

Ratings do not affect search result ranking. The aggregate data that would enable that is exposed through the API, but listing query semantics are unchanged.

Five endpoints serve the feature:

- `POST /api/ratings` - submit a rating; the caller must be authenticated, verified, and a participant of a completed transaction
- `GET /api/ratings/user/{user_id}` - the published ratings a user has received, plus their aggregate (public read)
- `GET /api/ratings/transaction/{transaction_id}` - the ratings attached to one transaction (participants only)
- `GET /api/ratings/eligibility/{transaction_id}` - whether the caller may rate, and why not if they may not
- `PATCH /api/ratings/{rating_id}/moderation` - move a rating between moderation states (administrator only)

For the data model, the full status-code matrix, the eligibility rules and the configuration tunables, please refer to the [Ratings and Reviews](./docs/features/ratings.md) documentation.

## API Documentation

For detailed API documentation, please refer to the [API Documentation](./docs/api.md) file.

## Contributing

We welcome contributions to the Used Car Marketplace project. Please read our [Contributing Guidelines](./CONTRIBUTING.md) for more information on how to get started.

## License

This project is licensed under the MIT License. See the [LICENSE](./LICENSE) file for details.

## Support

If you encounter any issues or have questions, please open an issue on our GitHub repository or contact our support team at support@usedcarmarketplace.com.