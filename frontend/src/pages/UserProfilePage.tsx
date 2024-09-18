import React, { useState, useEffect } from 'react';
import { ProfileForm } from '@/components/ProfileForm';
import { ListingManagement } from '@/components/ListingManagement';
import { fetchUserProfile, updateUserProfile } from '@/services/api';
import { useSelector, selectCurrentUser } from '@/store/userSlice';

const UserProfilePage: React.FC = () => {
  const [profileData, setProfileData] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const currentUser = useSelector(selectCurrentUser);

  useEffect(() => {
    const loadUserProfile = async () => {
      try {
        const data = await fetchUserProfile(currentUser.id);
        setProfileData(data);
      } catch (err) {
        setError('Failed to load user profile');
      } finally {
        setIsLoading(false);
      }
    };

    loadUserProfile();
  }, [currentUser.id]);

  const handleProfileUpdate = async (updatedData: any) => {
    try {
      const result = await updateUserProfile(currentUser.id, updatedData);
      setProfileData(result);
    } catch (err) {
      setError('Failed to update profile');
    }
  };

  if (isLoading) return <div>Loading...</div>;
  if (error) return <div>{error}</div>;

  return (
    <div className="user-profile-page">
      <h1>User Profile</h1>
      {profileData && (
        <ProfileForm
          initialData={profileData}
          onSubmit={handleProfileUpdate}
        />
      )}
      {currentUser.isSeller && (
        <ListingManagement userId={currentUser.id} />
      )}
      {/* HUMAN ASSISTANCE NEEDED
         Implement transaction history component. 
         This component is not defined in the current specification. */}
      <div className="transaction-history">
        <h2>Transaction History</h2>
        {/* Add transaction history component here */}
      </div>
    </div>
  );
};

export default UserProfilePage;