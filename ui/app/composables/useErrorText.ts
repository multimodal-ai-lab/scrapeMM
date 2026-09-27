/**
 * What an error type means, in a sentence. The server's messages are precise but long
 * and technical; in a list, the kind of failure is what somebody scans for, and the
 * message stays available in the tooltip.
 */
const DESCRIPTIONS: Record<string, string> = {
  TargetUnavailableError: 'The target was unavailable.',
  AccessBlockedError: 'Access to the content was blocked.',
  CaptchaEncounteredError: 'A CAPTCHA stood in the way.',
  RateLimitError: 'A rate limit was reached.',
  QuotaExceededError: 'A service quota is used up.',
  DomainBlacklistedError: 'The domain is blacklisted.',
  UnsupportedDomainError: 'This URL is not supported.',
  RetrievalFailed: 'The content could not be retrieved.',
  TimeoutError: 'The retrieval timed out.',
  DiskFull: 'The server ran out of disk space.',
}

export function describeError(type?: string | null): string {
  return (type && DESCRIPTIONS[type]) || 'An unexpected error occurred.'
}
