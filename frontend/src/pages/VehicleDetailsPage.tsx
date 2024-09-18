import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { PhotoGallery } from '@/components/PhotoGallery';
import { VehicleSpecs } from '@/components/VehicleSpecs';
import { MaintenanceHistory } from '@/components/MaintenanceHistory';
import { MessageBox } from '@/components/MessageBox';
import { PaymentForm } from '@/components/PaymentForm';
import { fetchListingDetails } from '@/services/api';

// HUMAN ASSISTANCE NEEDED
// The following component may need additional error handling, loading states, and responsive design considerations for production readiness.

const VehicleDetailsPage: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const [listingDetails, setListingDetails] = useState<any>(null);

  useEffect(() => {
    const fetchDetails = async () => {
      try {
        const details = await fetchListingDetails(id);
        setListingDetails(details);
      } catch (error) {
        console.error('Error fetching listing details:', error);
        // TODO: Implement proper error handling
      }
    };

    fetchDetails();
  }, [id]);

  if (!listingDetails) {
    return <div>Loading...</div>; // TODO: Replace with a proper loading component
  }

  return (
    <div className="vehicle-details-page">
      <h1>{listingDetails.title}</h1>
      <PhotoGallery photos={listingDetails.photos} />
      <VehicleSpecs specs={listingDetails.specs} />
      <MaintenanceHistory history={listingDetails.maintenanceHistory} />
      <MessageBox listingId={id} />
      <PaymentForm listingId={id} price={listingDetails.price} />
    </div>
  );
};

export default VehicleDetailsPage;