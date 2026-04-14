import os
def read_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

def list_files(path):
    return os.listdir(path)
