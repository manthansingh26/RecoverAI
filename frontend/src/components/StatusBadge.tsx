import { getStatusStyle, getStatusLabel } from '../utils/status';

interface StatusBadgeProps {
  status: string;
  size?: 'sm' | 'md';
}

export default function StatusBadge({ status, size = 'sm' }: StatusBadgeProps) {
  const style = getStatusStyle(status);
  const label = getStatusLabel(status);

  return (
    <span
      className={`inline-flex items-center rounded-full border font-medium ${style.bg} ${style.text} ${style.border} ${
        size === 'sm' ? 'px-2 py-0.5 text-[11px]' : 'px-2.5 py-1 text-xs'
      }`}
    >
      {label}
    </span>
  );
}
