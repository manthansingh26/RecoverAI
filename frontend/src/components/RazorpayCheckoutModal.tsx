import { useState, useCallback, useEffect, useRef } from 'react';
import {
  CreditCard,
  X,
  Loader2,
  CheckCircle2,
  AlertTriangle,
  ArrowRight,
  Sparkles,
  Info,
} from 'lucide-react';
import { createRazorpayOrder } from '../api/payments';
import { loadRazorpayScript } from '../utils/loadRazorpay';
import type { CreateOrderResponse } from '../types';

interface RazorpayCheckoutModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess?: () => void;
}

const PRESET_AMOUNTS = [
  { label: '₹499 (Standard)', value: 499, description: 'Standard failure test' },
  { label: '₹2,500 (Recoverable)', value: 2500, description: 'Smart recovery strategy' },
  { label: '₹55,000 (High Value)', value: 55000, description: 'Triggers human approval' },
];

export default function RazorpayCheckoutModal({
  isOpen,
  onClose,
  onSuccess,
}: RazorpayCheckoutModalProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  const [amount, setAmount] = useState<number>(499);
  const [customAmount, setCustomAmount] = useState<string>('499');
  const [isCustom, setIsCustom] = useState<boolean>(false);

  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [successInfo, setSuccessInfo] = useState<{
    paymentId: string;
    orderId: string;
  } | null>(null);
  const [dismissedInfo, setDismissedInfo] = useState<boolean>(false);

  // Dialog control
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    if (isOpen) {
      if (!dialog.open) {
        dialog.showModal();
      }
    } else {
      if (dialog.open) {
        dialog.close();
      }
    }
  }, [isOpen]);

  const handleClose = useCallback(() => {
    setLoading(false);
    setError(null);
    setSuccessInfo(null);
    setDismissedInfo(false);
    onClose();
  }, [onClose]);

  const handlePresetSelect = (val: number) => {
    setIsCustom(false);
    setAmount(val);
    setCustomAmount(String(val));
    setError(null);
    setSuccessInfo(null);
    setDismissedInfo(false);
  };

  const handleCustomChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setIsCustom(true);
    const valStr = e.target.value;
    setCustomAmount(valStr);
    const valNum = parseFloat(valStr);
    if (!isNaN(valNum) && valNum > 0) {
      setAmount(valNum);
      setError(null);
    }
  };

  const handleLaunchCheckout = useCallback(async () => {
    const selectedAmount = isCustom ? parseFloat(customAmount) : amount;
    if (isNaN(selectedAmount) || selectedAmount < 1) {
      setError('Please enter a valid amount of at least ₹1.00');
      return;
    }

    setLoading(true);
    setError(null);
    setSuccessInfo(null);
    setDismissedInfo(false);

    try {
      // 1. Load Razorpay Checkout.js script
      const scriptLoaded = await loadRazorpayScript();
      if (!scriptLoaded || !window.Razorpay) {
        throw new Error(
          'Failed to load Razorpay Checkout SDK. Please check your internet connection.',
        );
      }

      // 2. Create Real Test Order via Backend
      const order: CreateOrderResponse = await createRazorpayOrder({
        amount: selectedAmount,
        amount_in_rupees: true,
        currency: 'INR',
        receipt: `rcpt_${Date.now()}`,
        notes: {
          purpose: 'RecoverAI real webhook test',
        },
      });

      // 3. Open Razorpay Checkout
      const options = {
        key: order.key_id,
        amount: order.amount,
        currency: order.currency,
        name: 'RecoverAI',
        description: 'Real Test Mode Payment',
        order_id: order.order_id,
        prefill: {
          name: 'Demo Customer',
          email: 'demo@recoverai.local',
          contact: '9999999999',
        },
        notes: {
          purpose: 'RecoverAI real webhook test',
        },
        theme: {
          color: '#3b82f6',
        },
        modal: {
          ondismiss: () => {
            setLoading(false);
            setDismissedInfo(true);
            onSuccess?.();
          },
        },
        handler: (response: {
          razorpay_payment_id: string;
          razorpay_order_id: string;
          razorpay_signature: string;
        }) => {
          setLoading(false);
          setSuccessInfo({
            paymentId: response.razorpay_payment_id,
            orderId: response.razorpay_order_id,
          });
          onSuccess?.();
        },
      };

      const rzp = new window.Razorpay(options);
      rzp.on('payment.failed', (failResponse) => {
        setLoading(false);
        setDismissedInfo(true);
        // Note: Real recovery case comes exclusively via the Razorpay webhook, not client-side fabrication.
        if (failResponse?.error?.description) {
          setError(
            `Payment Failed: ${failResponse.error.description}. Awaiting backend webhook confirmation.`,
          );
        }
        onSuccess?.();
      });

      rzp.open();
    } catch (err) {
      setLoading(false);
      const message =
        err instanceof Error ? err.message : 'Failed to launch Razorpay checkout';
      setError(message);
    }
  }, [amount, customAmount, isCustom, onSuccess]);

  return (
    <dialog
      ref={dialogRef}
      onClose={onClose}
      className="rounded-2xl border border-border bg-bg-secondary p-0 shadow-2xl backdrop:bg-black/70 backdrop:backdrop-blur-sm w-full max-w-xl text-text-primary"
    >
      <div className="p-6">
        {/* Header */}
        <div className="flex items-start justify-between border-b border-border pb-4">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent-blue/15 text-accent-blue border border-accent-blue/30">
              <CreditCard className="h-5 w-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-base font-bold text-text-primary">
                  Test Real Razorpay Payment
                </h2>
                <span className="inline-flex items-center rounded-full bg-amber-500/10 border border-amber-500/30 px-2 py-0.5 text-[10px] font-semibold text-amber-400">
                  Test Mode
                </span>
              </div>
              <p className="mt-0.5 text-xs text-text-muted">
                Creates a real Razorpay Test Order to trigger the live payment.failed → webhook pipeline.
              </p>
            </div>
          </div>
          <button
            onClick={handleClose}
            className="rounded-lg p-1.5 text-text-muted hover:bg-bg-hover hover:text-text-primary transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Live Test Pipeline Flowchart (Requirement F) */}
        <div className="my-5 rounded-xl border border-border/80 bg-bg-primary/70 p-3.5">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-text-muted mb-2.5">
            End-to-End Recovery Flow
          </p>
          <div className="grid grid-cols-5 items-center gap-1 text-center text-[11px]">
            <div className="rounded-lg bg-bg-card border border-border p-2">
              <div className="font-semibold text-text-primary">1. Test Order</div>
              <div className="text-[9px] text-text-muted mt-0.5">Backend API</div>
            </div>
            <div className="flex justify-center text-text-muted">
              <ArrowRight className="h-3.5 w-3.5 text-accent-blue" />
            </div>
            <div className="rounded-lg bg-bg-card border border-border p-2">
              <div className="font-semibold text-accent-blue">2. Checkout</div>
              <div className="text-[9px] text-text-muted mt-0.5">Razorpay Modal</div>
            </div>
            <div className="flex justify-center text-text-muted">
              <ArrowRight className="h-3.5 w-3.5 text-red-400" />
            </div>
            <div className="rounded-lg bg-red-500/10 border border-red-500/30 p-2">
              <div className="font-semibold text-red-400">3. Fail Payment</div>
              <div className="text-[9px] text-text-muted mt-0.5">Simulate Failure</div>
            </div>
          </div>
          <div className="flex items-center justify-center gap-2 mt-2 pt-2 border-t border-border/50 text-[10px] text-text-secondary">
            <Sparkles className="h-3.5 w-3.5 text-accent-blue shrink-0" />
            <span>Webhook arrives at <code>/webhooks/razorpay</code> ➔ Live Dashboard updates</span>
          </div>
        </div>

        {/* Amount Selector */}
        <div className="mb-5">
          <label className="block text-xs font-semibold text-text-secondary mb-2">
            Select Order Amount
          </label>
          <div className="grid grid-cols-3 gap-2.5 mb-2.5">
            {PRESET_AMOUNTS.map((preset) => (
              <button
                key={preset.value}
                type="button"
                onClick={() => handlePresetSelect(preset.value)}
                className={`flex flex-col items-start rounded-xl border p-3 text-left transition-all ${
                  !isCustom && amount === preset.value
                    ? 'border-accent-blue bg-accent-blue/10 ring-1 ring-accent-blue/40'
                    : 'border-border bg-bg-card hover:bg-bg-hover hover:border-border/80'
                }`}
              >
                <span className="text-sm font-bold text-text-primary">
                  {preset.label.split(' ')[0]}
                </span>
                <span className="mt-0.5 text-[10px] text-text-muted">
                  {preset.description}
                </span>
              </button>
            ))}
          </div>

          <div className="flex items-center gap-2">
            <span className="text-xs text-text-muted">Or custom amount (₹):</span>
            <input
              type="number"
              min="1"
              max="1000000"
              value={customAmount}
              onChange={handleCustomChange}
              placeholder="e.g. 499"
              className="w-32 rounded-lg border border-border bg-bg-primary px-3 py-1.5 text-xs text-text-primary focus:border-accent-blue focus:outline-none"
            />
          </div>
        </div>

        {/* Test Mode Guidance Tip */}
        <div className="mb-5 flex items-start gap-2.5 rounded-xl border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-amber-200/90">
          <Info className="h-4 w-4 text-amber-400 shrink-0 mt-0.5" />
          <div className="space-y-1">
            <p className="font-semibold text-amber-400">
              How to test payment failure in Razorpay Checkout:
            </p>
            <p className="text-[11px] text-text-secondary leading-relaxed">
              When the Razorpay popup opens, select <strong>Netbanking</strong> (e.g. SBI/HDFC) or <strong>Card</strong>, then in the simulator screen click the red <strong>"Failure"</strong> button.
            </p>
          </div>
        </div>

        {/* Success Alert */}
        {successInfo && (
          <div className="mb-4 flex items-start gap-2.5 rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-3 text-xs text-emerald-300">
            <CheckCircle2 className="h-4 w-4 text-emerald-400 shrink-0 mt-0.5" />
            <div>
              <p className="font-semibold text-emerald-400">Payment Succeeded (Test Mode)</p>
              <p className="text-[11px] text-emerald-200/80 mt-0.5">
                Payment ID: <code className="text-emerald-300">{successInfo.paymentId}</code>. Successful payments do not trigger recovery. To test recovery intelligence, choose "Failure" next time.
              </p>
            </div>
          </div>
        )}

        {/* Dismissed / Failure Expected Alert */}
        {dismissedInfo && !error && !successInfo && (
          <div className="mb-4 flex items-start gap-2.5 rounded-xl border border-accent-blue/30 bg-accent-blue/10 p-3 text-xs text-accent-blue">
            <CheckCircle2 className="h-4 w-4 text-accent-blue shrink-0 mt-0.5" />
            <div>
              <p className="font-semibold text-text-primary">Checkout Closed</p>
              <p className="text-[11px] text-text-secondary mt-0.5">
                If you triggered a failure in Razorpay Test Mode, the webhook is being verified at <code>/webhooks/razorpay</code> and will appear on your live dashboard.
              </p>
            </div>
          </div>
        )}

        {/* Error Alert */}
        {error && (
          <div className="mb-4 flex items-start gap-2.5 rounded-xl border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-300">
            <AlertTriangle className="h-4 w-4 text-red-400 shrink-0 mt-0.5" />
            <div>
              <p className="font-semibold text-red-400">Checkout Error</p>
              <p className="text-[11px] text-red-200/80 mt-0.5">{error}</p>
            </div>
          </div>
        )}

        {/* Action Buttons */}
        <div className="flex items-center justify-end gap-3 pt-3 border-t border-border">
          <button
            type="button"
            onClick={handleClose}
            className="rounded-lg px-4 py-2 text-xs font-semibold text-text-muted hover:bg-bg-hover hover:text-text-primary transition-colors"
          >
            Close
          </button>
          <button
            type="button"
            onClick={handleLaunchCheckout}
            disabled={loading}
            className="flex items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2 text-xs font-semibold text-white shadow-sm transition-all hover:bg-emerald-500 active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                <span>Launching Checkout...</span>
              </>
            ) : (
              <>
                <CreditCard className="h-3.5 w-3.5" />
                <span>Open Razorpay Checkout (₹{isCustom ? customAmount : amount})</span>
              </>
            )}
          </button>
        </div>
      </div>
    </dialog>
  );
}
