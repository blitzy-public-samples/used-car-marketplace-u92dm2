import React, { useState, useEffect } from 'react';
import { useLocation } from 'react-router-dom';
import SearchBar from '@/components/SearchBar';
import ListingCard from '@/components/ListingCard';
import { searchListings } from '@/services/api';

const SearchResultsPage: React.FC = () => {
  const location = useLocation();
  const [searchResults, setSearchResults] = useState([]);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(true);

  const searchParams = new URLSearchParams(location.search);
  const query = searchParams.get('q') || '';

  useEffect(() => {
    fetchSearchResults();
  }, [query, page]);

  const fetchSearchResults = async () => {
    setLoading(true);
    try {
      const results = await searchListings(query, page);
      if (page === 1) {
        setSearchResults(results);
      } else {
        setSearchResults(prevResults => [...prevResults, ...results]);
      }
      setHasMore(results.length > 0);
    } catch (error) {
      console.error('Error fetching search results:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleLoadMore = () => {
    if (!loading && hasMore) {
      setPage(prevPage => prevPage + 1);
    }
  };

  return (
    <div className="search-results-page">
      <SearchBar initialQuery={query} />
      <div className="search-results-container">
        {searchResults.map(listing => (
          <ListingCard key={listing.id} listing={listing} />
        ))}
      </div>
      {loading && <div className="loading">Loading...</div>}
      {!loading && hasMore && (
        <button onClick={handleLoadMore} className="load-more-button">
          Load More
        </button>
      )}
      {!loading && !hasMore && searchResults.length > 0 && (
        <div className="no-more-results">No more results</div>
      )}
      {!loading && searchResults.length === 0 && (
        <div className="no-results">No results found</div>
      )}
    </div>
  );
};

export default SearchResultsPage;