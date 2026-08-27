// Re-exports of the canonical OperatorNotice types declared in
// app/api/operator-notice.types.ts.  Import from here when consuming
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
} from '../api/operator-notice.types';
