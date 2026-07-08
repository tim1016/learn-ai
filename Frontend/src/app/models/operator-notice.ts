// Re-exports of the canonical OperatorNotice types declared in
// app/api/live-instances.types.ts.  Import from here when consuming
// notice types outside the api layer (components, services, tests).
export type {
  OperatorIncident,
  OperatorNotice,
  OperatorNoticeAction,
  OperatorNoticeActionability,
  OperatorNoticeActionKind,
  OperatorNoticeCode,
  OperatorNoticeRemedyStatus,
  OperatorNoticeTier,
} from '../api/live-instances.types';
