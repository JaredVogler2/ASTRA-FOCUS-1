const role=document.querySelector('#login-role');
if(role)role.addEventListener('change',()=>{
  const kind={mechanic:'mechanics',lead:'teams',flm:'teams',super:'groups'}[role.value];
  const field=document.querySelector('#scope-field'),select=document.querySelector('#login-scope');
  field.hidden=!kind;
  select.replaceChildren();
  if(kind)select.append(document.querySelector('#scope-'+kind).content.cloneNode(true));
  else select.add(new Option('All','all'));
});
