import { api } from './client';
import type { CreateOrderRequest, CreateOrderResponse } from '../types';

/**
 * Creates a Razorpay Test Mode checkout order on the backend.
 *
 * Calls POST /api/payments/create-order which interacts with the official
 * Razorpay SDK using backend credentials and returns only safe frontend fields.
 */
export function createRazorpayOrder(
  payload: CreateOrderRequest,
): Promise<CreateOrderResponse> {
  return api.post<CreateOrderResponse>('/api/payments/create-order', payload);
}
