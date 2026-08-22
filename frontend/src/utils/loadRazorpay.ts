/**
 * Safe script loader for Razorpay Checkout.js (https://checkout.razorpay.com/v1/checkout.js).
 * Ensures the script is injected only once into document.body.
 */

let razorpayScriptPromise: Promise<boolean> | null = null;

export function loadRazorpayScript(): Promise<boolean> {
  if (typeof window === 'undefined') {
    return Promise.resolve(false);
  }

  // Already loaded and available on window
  if (typeof window.Razorpay === 'function') {
    return Promise.resolve(true);
  }

  // Return singleton promise to avoid multiple script tags
  if (razorpayScriptPromise) {
    return razorpayScriptPromise;
  }

  razorpayScriptPromise = new Promise<boolean>((resolve) => {
    // Check if script tag already exists in DOM
    const existingScript = document.querySelector<HTMLScriptElement>(
      'script[src="https://checkout.razorpay.com/v1/checkout.js"]',
    );
    if (existingScript) {
      if (typeof window.Razorpay === 'function') {
        resolve(true);
        return;
      }
      existingScript.addEventListener('load', () => resolve(true));
      existingScript.addEventListener('error', () => {
        razorpayScriptPromise = null;
        resolve(false);
      });
      return;
    }

    const script = document.createElement('script');
    script.src = 'https://checkout.razorpay.com/v1/checkout.js';
    script.async = true;
    script.onload = () => {
      resolve(true);
    };
    script.onerror = () => {
      razorpayScriptPromise = null;
      resolve(false);
    };
    document.body.appendChild(script);
  });

  return razorpayScriptPromise;
}
