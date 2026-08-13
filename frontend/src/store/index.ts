import { configureStore } from '@reduxjs/toolkit';
import { userReducer } from './userSlice';
import { listingReducer } from './listingSlice';
import { ratingReducer } from './ratingSlice';

const configureAppStore = () => {
  return configureStore({
    reducer: {
      user: userReducer,
      listing: listingReducer,
      rating: ratingReducer,
    },
  });
};

export const store = configureAppStore();

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;