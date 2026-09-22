// Admin password for end-to-end runs, read from the environment. There is no
// default password, and the one these tests used to hardcode was published.
export function adminPassword(): string {
  const pw = process.env.E2E_ADMIN_PASSWORD
  if (!pw) {
    throw new Error('Set E2E_ADMIN_PASSWORD to the admin password of the backend under test')
  }
  return pw
}
