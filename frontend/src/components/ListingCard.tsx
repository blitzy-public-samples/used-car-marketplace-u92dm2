import React from 'react';
import { Link } from 'react-router-dom';
import { formatCurrency, formatDate } from '@/utils/formatting';

interface VehicleListing {
  id: string;
  make: string;
  model: string;
  year: number;
  price: number;
  mileage: number;
  imageUrl: string;
  createdAt: string;
}

const ListingCard: React.FC<{ listing: VehicleListing }> = ({ listing }) => {
  return (
    <div className="listing-card">
      <img src={listing.imageUrl} alt={`${listing.year} ${listing.make} ${listing.model}`} className="listing-image" />
      <div className="listing-details">
        <h2>{`${listing.year} ${listing.make} ${listing.model}`}</h2>
        <p className="price">{formatCurrency(listing.price)}</p>
        <p className="mileage">{`${listing.mileage.toLocaleString()} miles`}</p>
        <p className="created-date">Listed on: {formatDate(listing.createdAt)}</p>
        <Link to={`/listings/${listing.id}`} className="view-details-link">
          View Details
        </Link>
      </div>
    </div>
  );
};

export default ListingCard;