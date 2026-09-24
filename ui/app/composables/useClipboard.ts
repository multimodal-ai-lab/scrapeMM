/**
 * Copies text to the clipboard, and says whether that actually happened.
 *
 * `navigator.clipboard` exists only in secure contexts -- HTTPS or localhost. A server
 * reached over plain http:// on the LAN is neither, so there the text goes through a
 * selected, off-screen textarea and the legacy copy command instead.
 */
export async function copyText(text: string): Promise<boolean> {
  if (!text) return false
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Denied (e.g. no permission or document not focused): try the fallback
    }
  }

  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.top = '-1000px'
  area.style.opacity = '0'
  document.body.appendChild(area)
  const previous = document.activeElement as HTMLElement | null
  area.select()
  area.setSelectionRange(0, text.length)
  let copied = false
  try {
    copied = document.execCommand('copy')
  } catch {
    copied = false
  }
  document.body.removeChild(area)
  previous?.focus?.()
  return copied
}
