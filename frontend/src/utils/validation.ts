import { z } from 'zod';
import { VehicleListingSchema } from '../schema/listing';
import sanitize from 'dompurify';

export function validateVehicleDetails(vehicleDetails: object) {
  const validatedData = VehicleListingSchema.parse(vehicleDetails);
  return validatedData;
}

export function sanitizeUserInput(input: string): string {
  return sanitize(input);
}