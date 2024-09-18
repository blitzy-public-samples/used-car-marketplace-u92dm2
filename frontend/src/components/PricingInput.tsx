import React, { useState, useEffect } from 'react';
import { fetchPricingSuggestions } from '@/services/api';
import { formatCurrency } from '@/utils/formatting';

interface PricingInputProps {
  listingDetails: VehicleListing;
  onPriceSet: (price: number) => void;
}

const PricingInput: React.FC<PricingInputProps> = ({ listingDetails, onPriceSet }) => {
  const [price, setPrice] = useState<string>('');
  const [suggestions, setSuggestions] = useState<number[]>([]);

  useEffect(() => {
    const fetchSuggestions = async () => {
      try {
        const pricingSuggestions = await fetchPricingSuggestions(listingDetails);
        setSuggestions(pricingSuggestions);
      } catch (error) {
        console.error('Error fetching pricing suggestions:', error);
      }
    };

    fetchSuggestions();
  }, [listingDetails]);

  const handlePriceChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value;
    setPrice(value);
  };

  const handlePriceSubmit = () => {
    const numericPrice = parseFloat(price.replace(/[^0-9.]/g, ''));
    if (!isNaN(numericPrice) && numericPrice > 0) {
      onPriceSet(numericPrice);
    } else {
      // HUMAN ASSISTANCE NEEDED
      // Consider adding error handling for invalid price input
      console.error('Invalid price input');
    }
  };

  return (
    <div className="pricing-input">
      <label htmlFor="price">Set Vehicle Price:</label>
      <input
        type="text"
        id="price"
        value={price}
        onChange={handlePriceChange}
        placeholder="Enter price"
      />
      <button onClick={handlePriceSubmit}>Set Price</button>
      
      {suggestions.length > 0 && (
        <div className="pricing-suggestions">
          <h4>Suggested Prices:</h4>
          <ul>
            {suggestions.map((suggestion, index) => (
              <li key={index} onClick={() => setPrice(formatCurrency(suggestion))}>
                {formatCurrency(suggestion)}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
};

export default PricingInput;