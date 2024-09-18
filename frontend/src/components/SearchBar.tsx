import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { sanitizeUserInput } from '@/utils/validation';

const SearchBar: React.FC = () => {
  const [searchQuery, setSearchQuery] = useState<string>('');
  const navigate = useNavigate();

  const handleInputChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const sanitizedInput = sanitizeUserInput(event.target.value);
    setSearchQuery(sanitizedInput);
  };

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (searchQuery.trim()) {
      navigate(`/search?q=${encodeURIComponent(searchQuery)}`);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="search-bar">
      <input
        type="text"
        value={searchQuery}
        onChange={handleInputChange}
        placeholder="Search for vehicles..."
        aria-label="Search for vehicles"
      />
      <button type="submit" aria-label="Submit search">
        Search
      </button>
    </form>
  );
};

export default SearchBar;