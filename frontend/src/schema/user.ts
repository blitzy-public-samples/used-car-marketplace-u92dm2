import { z } from 'zod';

export const UserSchema = z.object({
  id: z.string(),
  email: z.string().email(),
  firstName: z.string(),
  lastName: z.string(),
  role: z.string(),
  createdAt: z.date(),
  updatedAt: z.date(),
  isVerified: z.boolean(),
  ratingAverage: z.number().nullable(),
  ratingCount: z.number().int()
});

export type User = z.infer<typeof UserSchema>;