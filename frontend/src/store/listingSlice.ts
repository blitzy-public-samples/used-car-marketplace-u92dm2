import { createSlice, PayloadAction } from '@reduxjs/toolkit';

interface VehicleListing {
  // Define the properties of a VehicleListing here
  // This is a placeholder and should be replaced with the actual structure
  id: string;
  // Add other properties as needed
}

interface ListingState {
  listings: VehicleListing[];
  isLoading: boolean;
  error: string | null;
}

const initialState: ListingState = {
  listings: [],
  isLoading: false,
  error: null,
};

const listingSlice = createSlice({
  name: 'listing',
  initialState,
  reducers: {
    setListings: (state, action: PayloadAction<VehicleListing[]>) => {
      state.listings = action.payload;
    },
    setLoading: (state, action: PayloadAction<boolean>) => {
      state.isLoading = action.payload;
    },
    setError: (state, action: PayloadAction<string | null>) => {
      state.error = action.payload;
    },
  },
});

export const { setListings, setLoading, setError } = listingSlice.actions;
export default listingSlice.reducer;

// HUMAN ASSISTANCE NEEDED
// The VehicleListing interface needs to be properly defined with all necessary properties.
// Additional actions or thunks may be needed for API calls or complex state updates.
// Consider adding selectors for efficient state access if needed.