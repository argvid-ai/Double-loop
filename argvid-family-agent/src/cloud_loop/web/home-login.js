const homeForm = document.getElementById('homeLogin');
homeForm.addEventListener('submit', async event => {
  event.preventDefault();
  const button = document.getElementById('homeSubmit');
  const status = document.getElementById('homeStatus');
  button.disabled = true;
  status.textContent = '正在连接…';
  try {
    const response = await fetch('/access/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: document.getElementById('homeCode').value})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '连接失败，请稍后重试');
    location.replace('/');
  } catch (error) {
    status.textContent = error.message;
    button.disabled = false;
  }
});
