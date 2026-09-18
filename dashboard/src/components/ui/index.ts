/**
 * Shared UI primitives of the dashboard.
 *
 * Siblings import either this barrel or the individual module; the component
 * names and props are the cross-package contract.
 */

export { AppShell, type AppShellProps } from './app-shell';
export { Button, type ButtonProps, type ButtonSize, type ButtonVariant } from './button';
export { Card, type CardProps } from './card';
export { DataTable, type DataTableColumn, type DataTableProps } from './data-table';
export { EmptyState, type EmptyStateProps } from './empty-state';
export { ErrorBanner, type ErrorBannerProps } from './error-banner';
export { LiveToolbar, type LiveToolbarProps } from './live-toolbar';
export { StatTile, type StatTileProps, type StatTrend } from './stat-tile';
export {
  StatusBadge,
  orderStateTone,
  profileStatusTone,
  type StatusBadgeProps,
  type StatusTone,
} from './status-badge';
