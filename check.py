with open('app_gui.pyw', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'def on_editor_saved(new_bgr):' in line:
        print(f'{i-2}: {lines[i-2].rstrip()}')
        print(f'{i-1}: {lines[i-1].rstrip()}')
        print(f'{i}: {lines[i].rstrip()}')
        print(f'{i+1}: {lines[i+1].rstrip()}')
        print(f'{i+2}: {lines[i+2].rstrip()}')
        break
