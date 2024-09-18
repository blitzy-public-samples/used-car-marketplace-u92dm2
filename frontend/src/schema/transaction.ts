import { z } from 'zod';

export const TransactionSchema = z.object({
  id: z.string(),
  buyerId: z.string(),
  sellerId: z.string(),
  vehicleListingId: z.string(),
  amount: z.number(),
  status: z.string(),
  stripePaymentIntentId: z.string(),
  createdAt: z.date(),
  updatedAt: z.date(),
});

export type Transaction = z.infer<typeof TransactionSchema>;