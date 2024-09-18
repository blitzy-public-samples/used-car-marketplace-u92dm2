import React from 'react';
import ReactDOM from 'react-dom';
import { Provider } from 'react-redux';
import { Elements } from '@stripe/react-stripe-js';
import { loadStripe } from '@stripe/stripe-js';
import App from './App';
import { store } from './store';
import { setupInterceptors } from './services/api';

const STRIPE_PUBLIC_KEY = process.env.REACT_APP_STRIPE_PUBLIC_KEY;

const main = async () => {
  setupInterceptors();

  const stripePromise = loadStripe(STRIPE_PUBLIC_KEY!);

  const rootElement = document.getElementById('root');

  if (!rootElement) {
    console.error('Root element not found');
    return;
  }

  ReactDOM.render(
    <React.StrictMode>
      <Provider store={store}>
        <Elements stripe={stripePromise}>
          <App />
        </Elements>
      </Provider>
    </React.StrictMode>,
    rootElement
  );
};

main().catch((error) => {
  console.error('Failed to initialize the application:', error);
});