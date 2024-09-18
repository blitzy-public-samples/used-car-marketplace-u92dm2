import React, { useState } from 'react';
import { PhotoUploader } from '@/components/PhotoUploader';
import { VehicleDetailsForm } from '@/components/VehicleDetailsForm';
import { MaintenanceDocumentUploader } from '@/components/MaintenanceDocumentUploader';
import { PricingInput } from '@/components/PricingInput';
import { createListing } from '@/services/api';
import { compressImage } from '@/services/imageProcessing';

// HUMAN ASSISTANCE NEEDED
// The following component might need additional error handling, form validation, and UI/UX improvements for production readiness.
const ListingCreationPage: React.FC = () => {
  const [listingData, setListingData] = useState({
    photos: [],
    vehicleDetails: {},
    maintenanceDocuments: [],
    pricing: {},
  });
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState('');
  const [submitSuccess, setSubmitSuccess] = useState(false);

  const handlePhotoUpload = async (files: File[]) => {
    const compressedPhotos = await Promise.all(files.map(compressImage));
    setListingData(prev => ({ ...prev, photos: [...prev.photos, ...compressedPhotos] }));
  };

  const handleVehicleDetailsSubmit = (details: any) => {
    setListingData(prev => ({ ...prev, vehicleDetails: details }));
  };

  const handleMaintenanceDocumentUpload = (documents: File[]) => {
    setListingData(prev => ({ ...prev, maintenanceDocuments: [...prev.maintenanceDocuments, ...documents] }));
  };

  const handlePricingInput = (pricing: any) => {
    setListingData(prev => ({ ...prev, pricing }));
  };

  const handleSubmit = async () => {
    setIsSubmitting(true);
    setSubmitError('');
    setSubmitSuccess(false);

    try {
      await createListing(listingData);
      setSubmitSuccess(true);
    } catch (error) {
      setSubmitError('Failed to create listing. Please try again.');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="listing-creation-page">
      <h1>Create New Vehicle Listing</h1>
      <PhotoUploader onUpload={handlePhotoUpload} />
      <VehicleDetailsForm onSubmit={handleVehicleDetailsSubmit} />
      <MaintenanceDocumentUploader onUpload={handleMaintenanceDocumentUpload} />
      <PricingInput onInput={handlePricingInput} />
      <button onClick={handleSubmit} disabled={isSubmitting}>
        {isSubmitting ? 'Submitting...' : 'Create Listing'}
      </button>
      {submitError && <p className="error-message">{submitError}</p>}
      {submitSuccess && <p className="success-message">Listing created successfully!</p>}
    </div>
  );
};

export default ListingCreationPage;