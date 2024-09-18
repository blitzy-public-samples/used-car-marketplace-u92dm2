import React from 'react';
import { BrowserRouter as Router, Route, Switch } from 'react-router-dom';
import { Provider } from 'react-redux';
import Header from '@/components/Header';
import Footer from '@/components/Footer';
import HomePage from '@/pages/HomePage';
import ListingCreationPage from '@/pages/ListingCreationPage';
import SearchResultsPage from '@/pages/SearchResultsPage';
import VehicleDetailsPage from '@/pages/VehicleDetailsPage';
import UserProfilePage from '@/pages/UserProfilePage';
import TransactionPage from '@/pages/TransactionPage';
import { store } from '@/store';

const App: React.FC = () => {
  return (
    <Provider store={store}>
      <Router>
        <div className="app">
          <Header />
          <main>
            <Switch>
              <Route exact path="/" component={HomePage} />
              <Route path="/create-listing" component={ListingCreationPage} />
              <Route path="/search" component={SearchResultsPage} />
              <Route path="/vehicle/:id" component={VehicleDetailsPage} />
              <Route path="/profile" component={UserProfilePage} />
              <Route path="/transaction/:id" component={TransactionPage} />
            </Switch>
          </main>
          <Footer />
        </div>
      </Router>
    </Provider>
  );
};

export default App;