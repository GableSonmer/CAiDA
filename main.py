import os.path

import cv2

path = './data/office-31\\amazon\\images\\keyboard'
print(os.path.exists(path))
lst = os.listdir(path)
for image in lst:
    img = cv2.imread(os.path.join(path, image))
    print(img.shape)
