import { z } from 'zod';

export const VehicleListingSchema = z.object({
  id: z.string(),
  sellerId: z.string(),
  make: z.string(),
  model: z.string(),
  year: z.number().int().positive(),
  mileage: z.number().nonnegative(),
  price: z.number().positive(),
  condition: z.string(),
  photos: z.array(z.string().url()),
  maintenanceRecords: z.array(z.string()),
  status: z.string(),
  createdAt: z.date(),
  updatedAt: z.date()
});

export type VehicleListing = z.infer<typeof VehicleListingSchema>;