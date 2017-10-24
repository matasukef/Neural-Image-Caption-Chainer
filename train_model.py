import sys
import os
import math
import argparse
import numpy as np
import pickle
import chainer
from chainer import cuda
from chainer.cuda import cupy as cp
from chainer import optimizers, serializers

sys.path.append('./src')
from Image2CaptionDecoder import Image2CaptionDecoder
from DataLoader import DataLoader

import ENV
from slack_notification import post_slack

#add function to calculate accuracy

parser = argparse.ArgumentParser()
parser.add_argument('--gpu', '-g', type=int, default=0,
                    help="set GPU ID (negative value means using CPU)")
parser.add_argument('--dataset', '-d', type=str, default="./data/captions/processed/dataset_STAIR_jp.pkl",
                    help="Path to preprocessed caption pkl file")
parser.add_argument('--img_feature_root', '-f', type=str, default="./data/images/features/ResNet50/")
parser.add_argument('--img_root', '-i', type=str, default="./data/images/original/",
                    help="Path to image files root")
parser.add_argument('--output_dir', '-od', type=str, default="./data/train_data/STAIR2",
                    help="The directory to save model and log")
parser.add_argument('--preload', '-p', type=bool, default=True,
                    help="preload all image features onto RAM before trainig")
parser.add_argument('--epoch', type=int, default=100, 
                    help="The number of epoch")
parser.add_argument('--batch_size', type=int, default=256,
                    help="Mini batch size")
parser.add_argument('--hidden_dim', '-hd', type=int, default=512,
                    help="The number of hiden dim size in LSTM")
parser.add_argument('--img_feature_dim', '-fd', type=int, default=2048,
                    help="The number of image feature dim as input to LSTM")
parser.add_argument('--optimizer', '-opt', type=str, default="Adam", choices=['AdaDelta', 'AdaGrad', 'Adam', 'MomentumSGD', 'NesterovAG', 'RMSprop', 'RMSpropGraves', 'SGD', 'SMORMS3'],
                    help="Type of iptimizers")
parser.add_argument('--dropout_ratio', '-do', type=float, default=0.5,
                    help="Dropout ratio")
parser.add_argument('--n_layers', '-nl', type=int, default=1,
                    help="The number of layers")
parser.add_argument('--load_model', '-lm', type=int, default=0,
                    help="At which epoch you want to restart training(0 means training from zero)")
parser.add_argument('--slack', '-sl', type=bool, default=False,
                    help="Notification to slack")
args = parser.parse_args()

#create save directories
if not os.path.isdir(args.output_dir):
    os.makedirs(args.output_dir)
    os.mkdir(os.path.join(args.output_dir, 'models'))
    os.mkdir(os.path.join(args.output_dir, 'optimizers'))
    os.mkdir(os.path.join(args.output_dir, 'logs'))
    print('making some directories to ', args.output_dir)


#data preparation
print('loading preprocessed data...')

with open(args.dataset, 'rb') as f:
    data = pickle.load(f)

train_data = data['train']
val_data = data['val']
test_data = data['test']

#word dictionary
token2index = train_data['word_ids']

dataset = DataLoader(train_data, img_feature_root=args.img_feature_root, preload_features=args.preload, img_root=args.img_root)

#model preparation
model = Image2CaptionDecoder(vocab_size=len(token2index), hidden_dim=args.hidden_dim, img_feature_dim=args.img_feature_dim, dropout_ratio=args.dropout_ratio, n_layers=args.n_layers)


#cupy settings
if args.gpu >= 0:
    xp = cp
    cuda.get_device_from_id(args.gpu).use()
    model.to_gpu()
else:
    xp = np

opt = args.optimizer

#optimizers
if opt == 'SGD':
    optimizer = optimizers.SGD()
elif opt == 'AdaDelta':
    optimizer = optimizers.AdaDelta()
elif opt == 'Adam':
    optimizer = optimizers.Adam()
elif opt == 'AdaGrad':
    optimizer = optimizers.AdaGrad()
elif opt == 'MomentumSGD':
    optimizer = optimizers.MomentumSGD()
elif opt == 'NesterovAG':
    optimizer = optimizers.NesterovAG()
elif opt == 'RMSprop':
    optimizer = optimizers.RMSprop()
elif opt == 'RMSpropGraves':
    optimizer = optimizers.RMSpropGraves()
elif opt == 'SMORMS3':
    optimizer = optimizers.SMORMS3()

optimizer.setup(model)

if args.load_model:
    cap_model_path = os.path.join(args.output_dir, 'models', 'caption_model' + str(args.load_model) + '.model')
    opt_model_path = os.path.join(args.output_dir, 'optimizers', 'optimizer' + str(args.load_model) + '.model')
    serializers.load_hdf5(cap_model_path, model)
    serializers.load_hdf5(opt_model_path, optimizer)
    
    dataset.epoch = args.load_model + 1

# configuration about training
total_epoch = args.epoch
batch_size = args.batch_size
caption_size = dataset.caption_size
total_iteration = math.ceil(caption_size / batch_size)
img_size = dataset.img_size
hidden_dim = args.hidden_dim
num_layers = args.n_layers
sum_loss = 0
iteration = 0
accuracy = 0


sen_title = '-----configurations-----'
sen_gpu = 'GPU ID: ' + str(args.gpu)
sen_img = 'Total images: ' + str(img_size)
sen_cap = 'Total captions: ' + str(caption_size)
sen_epoch = 'Total epoch: ' + str(total_epoch)
sen_batch = 'Batch size: ' + str(batch_size)
sen_hidden = 'The number of hidden dim: ' + str(hidden_dim)
sen_LSTM = 'The number of LSTM layers: ' + str(num_layers)
sen_optimizer = 'Optimizer: ' + str(opt)
sen_learnning = 'Learning rate: '

sen_conf = sen_title + '\n' + sen_gpu + '\n' + sen_img + '\n' + sen_cap + '\n' + sen_epoch + '\n' + sen_batch + '\n' + sen_hidden + '\n' + sen_LSTM + '\n' + sen_optimizer + '\n'

# before training
print(sen_conf)

with open(os.path.join(args.output_dir, 'logs', 'configurations.txt'), 'w') as f:
    f.write(sen_conf)

#start training

print('\nepoch 1')

while dataset.now_epoch <= total_epoch:
    
    model.cleargrads()
    
    now_epoch = dataset.now_epoch
    img_batch, cap_batch = dataset.get_batch(batch_size)
    
    if args.gpu >= 0:
        img_batch = cuda.to_gpu(img_batch, device=args.gpu)
        cap_batch = [ cuda.to_gpu(x, device=args.gpu) for x in cap_batch]

    #lstml inputs
    hx = xp.zeros((num_layers, batch_size, model.hidden_dim), dtype=xp.float32)
    cx = xp.zeros((num_layers, batch_size, model.hidden_dim), dtype=xp.float32)

    loss = model(hx, cx, cap_batch)

    loss.backward()
    
    #update parameters
    optimizer.update()

    sum_loss += loss.data * batch_size
    iteration += 1

    print('epoch: {0} iteration: {1}, loss: {2}'.format(now_epoch, str(iteration) + '/' + str(total_iteration), round(float(loss.data), 10)))
    if now_epoch is not dataset.now_epoch:
        print('new epoch phase')
        mean_loss = sum_loss / caption_size

        print('\nepoch {0} result'.format(now_epoch-1))
        print('epoch: {0} loss: {1}'.format(now_epoch, round(float(mean_loss), 10)))
        print('\nepoch ', now_epoch)

        serializers.save_hdf5(os.path.join(args.output_dir, 'models', 'caption_model' + str(now_epoch) + '.model'), model)
        serializers.save_hdf5(os.path.join(args.output_dir, 'optimizers', 'optimizer' + str(now_epoch) + '.model'), optimizer)
        
        with open(os.path.join(args.output_dir, 'logs', 'mean_loss.txt'), 'a') as f:
            f.write(str(mean_loss) + '\n')

        if args.slack:
            name = args.output_dir
            if name[-1] == '/':
                name =name[:-1]
            name = os.path.basename(name)
            text = 'epoch: ' + str(now_epoch-1) + ' loss: ' + str(mean_loss)
            #ENV.POST_URL is set at ENV.py
            post_slack(ENV.POST_URL, name, text)
        sum_loss = 0
        iteration = 0
