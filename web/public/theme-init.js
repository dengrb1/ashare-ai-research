(() => {
  let mode = 'system'
  try {
    const saved = localStorage.getItem('ashare-theme')
    if (saved === 'light' || saved === 'dark' || saved === 'system') mode = saved
  } catch {}
  const resolved = mode === 'system' && window.matchMedia?.('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : mode === 'dark'
      ? 'dark'
      : 'light'
  document.documentElement.dataset.theme = resolved
  document.documentElement.style.colorScheme = resolved
  const theme = document.querySelector('meta[name="theme-color"]')
  if (theme) theme.content = resolved === 'dark' ? '#071018' : '#f4f7f8'
})()
