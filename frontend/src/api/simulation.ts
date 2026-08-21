import { api } from './client';
import type { SimulationScenario, SimulationResult } from '../types';

export function simulatePaymentFailure(
  scenario: SimulationScenario,
): Promise<SimulationResult> {
  return api.post<SimulationResult>('/api/dev/simulate-payment-failure', {
    scenario,
  });
}
