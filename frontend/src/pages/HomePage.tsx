import React, { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { useSelector } from 'react-redux';
import SearchBar from '@/components/SearchBar';
import ListingCard from '@/components/ListingCard';
import { fetchListings } from '@/services/api';
import { selectFeaturedListings } from '@/store/listingSlice';

const HomePage: React.FC = () => {
  const [featuredListings, setFeaturedListings] = useState([]);
  const featuredListingsFromStore = useSelector(selectFeaturedListings);

  useEffect(() => {
    const getFeaturedListings = async () => {
      try {
        const listings = await fetchListings({ featured: true });
        setFeaturedListings(listings);
      } catch (error) {
        console.error('Error fetching featured listings:', error);
      }
    };

    if (featuredListingsFromStore.length === 0) {
      getFeaturedListings();
    } else {
      setFeaturedListings(featuredListingsFromStore);
    }
  }, [featuredListingsFromStore]);

  return (
    <div className="home-page">
      <h1>Welcome to Used Car Marketplace</h1>
      <SearchBar />
      <section className="featured-listings">
        <h2>Featured Listings</h2>
        <div className="listing-grid">
          {featuredListings.map((listing) => (
            <ListingCard key={listing.id} listing={listing} />
          ))}
        </div>
      </section>
      <Link to="/listings" className="view-all-link">
        View All Listings
      </Link>
    </div>
  );
};

export default HomePage;