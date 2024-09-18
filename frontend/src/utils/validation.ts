import { z } from 'zod';
import { VehicleListingSchema } from '../schema/listing';
import sanitize from 'dompurify';

export const validateVehicleDetails = (vehicleDetails: any) => {
  const validatedData = VehicleListingSchema.parse(vehicleDetails);
  return validatedData;
};

export const sanitizeUserInput = (input: string): string => {
  return sanitize(input);
};