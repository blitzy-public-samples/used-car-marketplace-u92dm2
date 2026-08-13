import { configureStore } from '@reduxjs/toolkit';
/*
 * DEFAULT imports for these two, because that is the only thing either module
 * exports its reducer as.
 *
 * `./userSlice` exports `{ setUser, setLoading, setError }` plus
 * `export default userSlice.reducer`, and `./listingSlice` exports
 * `{ setListings, setLoading, setError }` plus `export default
 * listingSlice.reducer`. Neither declares a named `userReducer` or
 * `listingReducer`, so the named form these lines previously used resolved to
 * nothing: it produced two `TS2614` errors and, at runtime, `undefined` for two
 * of the three entries in the reducer map below — and `configureStore` throws
 * when a slice reducer is `undefined`. The store could not be constructed at all,
 * which took `state.rating` down with it even though the rating slice itself was
 * correct.
 *
 * The two sibling slices are deliberately NOT changed to add named exports. They
 * are outside this feature's scope, three other modules already consume their
 * default export, and adding a second alias for the same value would leave two
 * ways to import one reducer. The import site is the thing that was wrong, so the
 * import site is what changed.
 *
 * `./ratingSlice` stays a NAMED import: it exports `ratingReducer` as a named
 * export as well as a default, precisely so it resolves under either style, and
 * the named form states which reducer is meant at the point of use.
 */
import userReducer from './userSlice';
import listingReducer from './listingSlice';
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
