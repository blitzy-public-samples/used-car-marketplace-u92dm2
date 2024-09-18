import { configureStore } from '@reduxjs/toolkit';
import { userReducer } from './userSlice';
import { listingReducer } from './listingSlice';

const configureAppStore = () => {
  return configureStore({
    reducer: {
      user: userReducer,
      listing: listingReducer,
    },
  });
};

export const store = configureAppStore();

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;