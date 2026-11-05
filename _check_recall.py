import time
import re
from app import app

with app.test_client() as c:
    with c.session_transaction() as sess:
        sess['user_id'] = 'test-admin'
        sess['is_admin'] = True
        sess['admin_verified_at'] = time.time()
    html = c.get('/crm').get_data(as_text=True)

idx_section = html.find('Datas de recompra')
idx_actions = html.find('Ações Rápidas')
print('section before actions', idx_section < idx_actions)
chunk = html[idx_section:idx_actions]
names = re.findall(
    r'hover:text-indigo-700">([^<]+)</a>',
    chunk,
)
print('customer links', len(names))
for name in names[:10]:
    print('-', name)
print('has 1 ano preset', '1 ano' in chunk)
print('has Faltam header', 'Faltam' in chunk)
