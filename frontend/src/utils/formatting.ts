import { format } from 'date-fns';

export function formatCurrency(amount: number, currencyCode: string): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: currencyCode,
  }).format(amount);
}

export function formatDate(date: Date | string): string {
  return format(new Date(date), 'MMMM d, yyyy');
}