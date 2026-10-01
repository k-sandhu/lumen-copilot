/** Clear every native representation, including React's reflected default. */
export function scrubCredentialInput(input: HTMLInputElement | HTMLTextAreaElement): void {
  input.value = '';
  input.defaultValue = '';
  input.removeAttribute('value');
  input.removeAttribute('defaultvalue');
  if (
    input instanceof HTMLInputElement &&
    (input.type === 'password' || /^(current-password|new-password)$/.test(input.autocomplete))
  ) {
    input.type = 'password';
  }
}
