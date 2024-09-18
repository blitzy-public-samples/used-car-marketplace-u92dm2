import { z } from 'zod';

export const MessageSchema = z.object({
  id: z.string(),
  senderId: z.string(),
  receiverId: z.string(),
  vehicleListingId: z.string(),
  content: z.string(),
  read: z.boolean(),
  createdAt: z.date()
});

export type Message = z.infer<typeof MessageSchema>;