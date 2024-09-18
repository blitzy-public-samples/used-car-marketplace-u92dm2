import React from 'react';
import { Link } from 'react-router-dom';
import { useSelector } from 'react-redux';
import SearchBar from '@/components/SearchBar';
import { selectCurrentUser } from '@/store/userSlice';

const Header: React.FC = () => {
  const currentUser = useSelector(selectCurrentUser);

  return (
    <header className="bg-white shadow-md">
      <div className="container mx-auto px-4 py-3 flex items-center justify-between">
        <div className="flex items-center">
          <Link to="/" className="text-2xl font-bold text-blue-600">
            Used Car Marketplace
          </Link>
          <nav className="ml-6">
            <Link to="/cars" className="mx-2 text-gray-600 hover:text-blue-600">
              Browse Cars
            </Link>
            <Link to="/sell" className="mx-2 text-gray-600 hover:text-blue-600">
              Sell Your Car
            </Link>
          </nav>
        </div>
        <div className="flex items-center">
          <SearchBar />
          {currentUser ? (
            <div className="ml-4">
              <Link to="/profile" className="text-gray-600 hover:text-blue-600">
                {currentUser.name}
              </Link>
              {/* HUMAN ASSISTANCE NEEDED: Add logout functionality */}
              {/* <button onClick={handleLogout} className="ml-2 text-gray-600 hover:text-blue-600">
                Logout
              </button> */}
            </div>
          ) : (
            <div className="ml-4">
              <Link to="/login" className="text-gray-600 hover:text-blue-600 mr-2">
                Login
              </Link>
              <Link to="/register" className="text-gray-600 hover:text-blue-600">
                Register
              </Link>
            </div>
          )}
        </div>
      </div>
    </header>
  );
};

export default Header;