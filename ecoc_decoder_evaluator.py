import torch
import sys
import numpy as np
import os
import yaml
import matplotlib.pyplot as plt
import torchvision

from torch.utils.data import DataLoader
import torchvision.transforms as transforms
from torchvision import datasets
import torch.nn as nn
import argparse
import logging
from models.resnet_ecoc_simclr import ResNetECOCSimCLR
from torch.utils.tensorboard import SummaryWriter
import faiss

parser = argparse.ArgumentParser(description='PyTorch SimCLR')
parser.add_argument('-folder_name', default='cifar10-lars-v5.2-1-batch512',
                    help='model file name')
parser.add_argument('--epochs', default=200, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--pretrain_epochs', default=1000, type=int, metavar='N',
                    help='number of total epochs to run')
def get_stl10_data_loaders(download, shuffle=False, batch_size=256):
  train_dataset = datasets.STL10('./datasets', split='train', download=download,
                                  transform=transforms.ToTensor())

  train_loader = DataLoader(train_dataset, batch_size=batch_size,
                            num_workers=0, drop_last=False, shuffle=shuffle)

  test_dataset = datasets.STL10('./datasets', split='test', download=download,
                                  transform=transforms.ToTensor())

  test_loader = DataLoader(test_dataset, batch_size=2*batch_size,
                            num_workers=10, drop_last=False, shuffle=shuffle)
  return train_loader, test_loader

def get_cifar10_data_loaders(download, shuffle=False, batch_size=256):
  train_dataset = datasets.CIFAR10('./datasets', train=True, download=download,
                                  transform=transforms.ToTensor())
  # split train data into data for generate codeword and for training.
  codeword_data_size = int(0.2*len(train_dataset))
  train_size = len(train_dataset) - codeword_data_size
  lengths = [codeword_data_size, train_size]
  codeword_gen_dataset, train_dataset = torch.utils.data.dataset.random_split(train_dataset, lengths)

  codeword_gen_loader = DataLoader(codeword_gen_dataset, batch_size=batch_size,
                            num_workers=0, drop_last=False, shuffle=shuffle)
  train_loader = DataLoader(train_dataset, batch_size=batch_size,
                            num_workers=0, drop_last=False, shuffle=shuffle)
  test_dataset = datasets.CIFAR10('./datasets', train=False, download=download,
                                  transform=transforms.ToTensor())

  test_loader = DataLoader(test_dataset, batch_size=2*batch_size,
                            num_workers=10, drop_last=False, shuffle=shuffle)
  return codeword_gen_loader, train_loader, test_loader

def accuracy(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res
    

def generate_codeword(model, data, class_num=10, out_dim=2048):
  # Record the codeword for each category
  codewords = np.zeros((class_num, out_dim))
  # Record how many samples there are for each category
  num_of_class = np.zeros(class_num)
  for x, y in data:
    x = x.to(device)
    # Get outputs of ecoc encoder
    logits = model(x)
    for c in range(class_num):
      # Get the sample index belonging to class c
      yi = (y == c).nonzero(as_tuple=True)
      num_of_class[c] += len(yi[0])
      # Sum the current codeword of class c
      codewords[c,:] += torch.sum(logits[yi],dim=0).data.cpu().numpy()
  # Average the codeword
  for i in range(class_num):
    codewords[i] /= num_of_class[i]

  # convert code to -1 and 1
  # codewords[codewords>0]=1
  # codewords[codewords<=0]=-1
  # for i in range(10):
  #   for j in range(i, 10):
  #     c = 0
  #     if i == j:
  #       continue
  #     for k in range(2048):
  #       c += abs(codewords[i,k]-codewords[j,k])
  #     print('class {0}, and class {1} have {2} differents.'.format(i,j,c))
  # return torch.tensor(codewords, dtype=torch.float32, device=torch.device('cuda:0'),  requires_grad=True)
  return codewords
def calculate_cosine_similarity(features, codewords):
    codewords = torch.tensor(codewords, dtype=torch.float32, device=torch.device('cuda:0'),  requires_grad=True)
    # normalize features and codewords by their L2 norm
    features = features / torch.norm(features, dim=1, keepdim=True)
    codewords = codewords / torch.norm(codewords, dim=1, keepdim=True)
    return torch.einsum('ij,kj->ik', features, codewords)

def get_logits_test(features, codewords, out_dim=10):
    # Given that cos_sim(u, v) = dot(u, v) / (norm(u) * norm(v))
    #                          = dot(u / norm(u), v / norm(v))
    # We fist normalize the rows, before computing their dot products via transposition:
    a_norm = features / features.norm(dim=1)[:, None]
    b_norm = codewords / codewords.norm(dim=1)[:, None]
    res = torch.mm(a_norm, b_norm.transpose(0,1))
    # print(res[0,:])
    return torch.tensor(res, dtype=torch.float32, device=torch.device('cuda:0'),  requires_grad=True)
    # logits = nn.functional.cosine_similarity(features, codewords, dim=-1)
    # cos = nn.CosineSimilarity(dim=1, eps=1e-6)
    # logits = cos(features, codewords)
    # print(logits.shape)
    # return torch.tensor(logits, dtype=torch.float32, device=torch.device('cuda:0'),  requires_grad=True)

def get_logits(features, codewords, out_dim=10):
    os.environ["KMP_DUPLICATE_LIB_OK"]="TRUE"
    # consor data type from tensor to np array
    # np_features = features.detach().cpu().numpy().astype('float32')
    np_features = features.data.cpu().numpy().astype('float32')
    # Eliminate warning
    codebook = np.zeros((39, 2048)).astype('float32')
    codebook[:10,:] = codewords.astype('float32')
    codewords = codebook
    # cosine similarity search using faiss tool
    vector_dim = codewords.shape[1]
    faiss.normalize_L2(np_features)
    faiss.normalize_L2(codewords)
    
    nlist = 1
    quantizer = faiss.IndexFlatL2(vector_dim) 
    index = faiss.IndexIVFFlat(quantizer, vector_dim, nlist, faiss.METRIC_INNER_PRODUCT) 
    index.nprobe = 1
    index.train(codewords) 
    index.add(codewords)
    D, I = index.search(np_features, out_dim)

    ind = I.argsort(axis=-1)
    # Order according to the class(not similarity)
    logits = np.take_along_axis(D, ind, axis=-1)
    return torch.tensor(logits, dtype=torch.float32, device=torch.device('cuda:0'),  requires_grad=True)

def get_loss(features, codewords, labels):
    loss = 0
    for i in range(features.shape[0]):
      cos = torch.nn.CosineSimilarity(dim=0)
      y = torch.tensor(codewords[labels[i]], dtype=torch.float32, device=torch.device('cuda:0'),  requires_grad=True)
      loss += max(1-cos(features[i], y),0)
    return torch.tensor(loss, dtype=torch.float32, device=torch.device('cuda:0'),  requires_grad=True)
def get_acc(logits, labels):
  counts = 0
  for i in range(len(logits)):
    pred_class = torch.argmax(logits[i,:])
    if pred_class == labels:
      counts += 1
  return counts/len(logits)


if __name__ == '__main__':
  device = 'cuda' if torch.cuda.is_available() else 'cpu'
  print("Using device:", device)
  args = parser.parse_args()
  writer = SummaryWriter()
  # Load config.yml
  with open(os.path.join('./runs/{0}/config.yml'.format(args.folder_name))) as file:
    config = yaml.load(file, Loader=yaml.Loader)

  # cp_epoch = (4-len(str(config.epochs)))*'0' + str(config.epochs)
  cp_epoch = '{:04d}'.format(args.pretrain_epochs)
  
  # Get baseline model arch
  if config.arch == 'resnet18':
    model = torchvision.models.resnet18(pretrained=False, num_classes=10).to(device)
  elif config.arch == 'resnet50':
    if config.model_version == 5:
      model = ResNetECOCSimCLR(base_model=config.arch, out_dim=10)
      
      # Remove last relu
      model.ecoc_encoder[1]= nn.Identity()
      model.fc = nn.Identity()
      # dim_mlp = model.ecoc_encoder[0].out_features
      # model.fc = nn.Linear(dim_mlp, 10)
    else:
      model = torchvision.models.resnet50(pretrained=False, num_classes=10).to(device)
      dim_mlp = model.fc.in_features
      model.conv1 = nn.Conv2d(3, 64, 3, 1, 1, bias=False)
      model.maxpool = nn.Identity()
    print(model)


  model.cuda()
  # print(model)
  # Load weight file
  checkpoint = torch.load('./runs/{0}/checkpoint_{1}.pth.tar'.format(args.folder_name, cp_epoch), map_location=device)
  state_dict = checkpoint['state_dict']
  state_dict_cpy = state_dict.copy()

  # remove prefix
  if config.model_version == 5:
    for k in list(state_dict.keys()):
      if k.startswith('fc.'):
        del state_dict[k]
  else:
    for k in list(state_dict.keys()):
      if k.startswith('backbone.'):
        if k.startswith('backbone') and not k.startswith('backbone.fc'):
          # remove prefix
          state_dict[k[len("backbone."):]] = state_dict[k]
      del state_dict[k]

  log = model.load_state_dict(state_dict, strict=False)
  # assert log.missing_keys == []

  # Load dataset to loader
  if config.dataset_name == 'cifar10':
    codeword_gen_loader, train_loader, test_loader = get_cifar10_data_loaders(download=True)
  elif config.dataset_name == 'stl10':
    train_loader, test_loader = get_stl10_data_loaders(download=True)
  print("Dataset:", config.dataset_name)
  codewords = generate_codeword(model, codeword_gen_loader, class_num=10, out_dim=2048)

  requires_grad_list = ['ecoc_encoder.0.weight', 'ecoc_encoder.0.bias'] if config.model_version == 5 else []

  # freeze all layers but the last fc
  for name, param in model.named_parameters():
      # print(name, param)
      if name not in requires_grad_list:
          param.requires_grad = False
      else:
          param.requires_grad = True
  parameters = list(filter(lambda p: p.requires_grad, model.parameters()))
  assert len(parameters) == len(requires_grad_list)

  # Assign some model settings
  optimizer = torch.optim.Adam(model.parameters(), lr=3e-4, weight_decay=0.0008)
  # optimizer = torch.optim.Adam(model.parameters(), lr=5, weight_decay=0.0008)
  criterion = torch.nn.CrossEntropyLoss().to(device)

  # training & testing
  logging.basicConfig(filename=os.path.join('./runs/{0}'.format(args.folder_name), 'ecocdec_eval_{0}.log'.format(cp_epoch)), level=logging.DEBUG)
  
  for epoch in range(args.epochs):
    top1_train_accuracy = 0
    selfacc = 0
    # training
    for counter, (x_batch, y_batch) in enumerate(train_loader):
      
      x_batch = x_batch.to(device)
      y_batch = y_batch.to(device)

      # loss = get_loss(model(x_batch), codewords, labels=y_batch)
      # print(model.ecoc_encoder[0].weight)
      # logits = get_logits(model(x_batch), codewords)
      # logits = get_logits(model(x_batch), codewords)
      logits = calculate_cosine_similarity(model(x_batch), codewords)
      loss = criterion(logits, y_batch)#.add(get_loss(model(x_batch), codewords, labels=y_batch))
      # print(loss)
      top1 = accuracy(logits, y_batch, topk=(1,))

      top1_train_accuracy += top1[0]
      optimizer.zero_grad()
      loss.backward()
      optimizer.step()
      # print(list(model.parameters())[-3].requires_grad)
      # check if weights are updated
      # a = list(model.parameters())[0].clone()
      # loss.backward()
      # optimizer.step()
      # b = list(model.parameters())[0].clone()
      # print(torch.equal(a.data, b.data))
    top1_train_accuracy /= (counter + 1)
    top1_accuracy = 0
    top5_accuracy = 0
    # testing
    for counter, (x_batch, y_batch) in enumerate(test_loader):
      x_batch = x_batch.to(device)
      y_batch = y_batch.to(device)

      # logits = get_logits(model(x_batch), codewords)
      logits = calculate_cosine_similarity(model(x_batch), codewords)

      top1, top5 = accuracy(logits, y_batch, topk=(1,5))
      top1_accuracy += top1[0]
      top5_accuracy += top5[0]

    top1_accuracy /= (counter + 1)
    top5_accuracy /= (counter + 1)

    writer.add_scalar('loss', loss, global_step=epoch)
    writer.add_scalar('Eval Train: acc/top1', top1_accuracy, global_step=epoch)
    writer.add_scalar('Eval: acc/top1', top1_accuracy, global_step=epoch)
    writer.add_scalar('Eval: acc/top5', top5_accuracy, global_step=epoch)
    
    logging.info(f"Epoch {epoch}\tTop1 Train accuracy {top1_train_accuracy.item()}\tTop1 Test accuracy: {top1_accuracy.item()}\tTop5 test acc: {top5_accuracy.item()}")
    print(f"Epoch {epoch}\tTop1 Train accuracy {top1_train_accuracy.item()}\tTop1 Test accuracy: {top1_accuracy.item()}\tTop5 test acc: {top5_accuracy.item()}")