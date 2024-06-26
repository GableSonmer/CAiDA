import argparse
import os, sys
import os.path as osp
import torchvision
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from numpy import linalg as LA
from torchvision import transforms
import network, loss
from torch.utils.data import DataLoader
from data_list import ImageList, ImageList_idx
import random, pdb, math, copy
from tqdm import tqdm
from scipy.spatial.distance import cdist
from sklearn.metrics import confusion_matrix


def op_copy(optimizer):
    for param_group in optimizer.param_groups:
        param_group['lr0'] = param_group['lr']
    return optimizer


def lr_scheduler(optimizer, iter_num, max_iter, gamma=10, power=0.75):
    decay = (1 + gamma * iter_num / max_iter) ** (-power)
    for param_group in optimizer.param_groups:
        param_group['lr'] = param_group['lr0'] * decay
        param_group['weight_decay'] = 1e-3
        param_group['momentum'] = 0.9
        param_group['nesterov'] = True
    return optimizer


def image_train(resize_size=256, crop_size=224, alexnet=False):
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    return transforms.Compose([
        transforms.Resize((resize_size, resize_size)),
        transforms.RandomCrop(crop_size),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        normalize
    ])


def image_test(resize_size=256, crop_size=224, alexnet=False):
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    return transforms.Compose([
        transforms.Resize((resize_size, resize_size)),
        transforms.CenterCrop(crop_size),
        transforms.ToTensor(),
        normalize
    ])


def data_load(args):
    ## prepare data
    dsets = {}
    dset_loaders = {}
    train_bs = args.batch_size
    txt_tar = open(args.t_dset_path).readlines()
    txt_test = open(args.test_dset_path).readlines()

    dsets["target"] = ImageList_idx(txt_tar, transform=image_train())
    dset_loaders["target"] = DataLoader(dsets["target"], batch_size=train_bs, shuffle=True, num_workers=args.worker,
                                        drop_last=False)
    dsets['target_'] = ImageList_idx(txt_tar, transform=image_train())
    dset_loaders['target_'] = DataLoader(dsets['target_'], batch_size=train_bs * 3, shuffle=False,
                                         num_workers=args.worker, drop_last=False)
    dsets["test"] = ImageList_idx(txt_test, transform=image_test())
    dset_loaders["test"] = DataLoader(dsets["test"], batch_size=train_bs * 3, shuffle=False, num_workers=args.worker,
                                      drop_last=False)

    return dset_loaders


def train_target(args):
    dset_loaders = data_load(args)
    ## set base network
    if args.net[0:3] == 'res':
        netF_list = [network.ResBase(res_name=args.net).cuda() for i in range(len(args.src))]
    elif args.net[0:3] == 'vgg':
        netF_list = [network.VGGBase(vgg_name=args.net).cuda() for i in range(len(args.src))]

    netB_list = [network.feat_bottleneck(type=args.classifier, feature_dim=netF_list[i].in_features,
                                         bottleneck_dim=args.bottleneck).cuda() for i in range(len(args.src))]
    netC_list = [
        network.feat_classifier(type=args.layer, class_num=args.class_num, bottleneck_dim=args.bottleneck).cuda() for i
        in range(len(args.src))]

    netQ = network.source_quantizer(source_num=len(args.src)).cuda()

    param_group = []
    for i in range(len(args.src)):
        modelpath = args.output_dir_src[i] + '/source_F.pt'
        print(modelpath)
        netF_list[i].load_state_dict(torch.load(modelpath))
        netF_list[i].eval()
        for k, v in netF_list[i].named_parameters():
            param_group += [{'params': v, 'lr': args.lr * args.lr_decay1}]

        modelpath = args.output_dir_src[i] + '/source_B.pt'
        print(modelpath)
        netB_list[i].load_state_dict(torch.load(modelpath))
        netB_list[i].eval()
        for k, v in netB_list[i].named_parameters():
            param_group += [{'params': v, 'lr': args.lr * args.lr_decay2}]

        modelpath = args.output_dir_src[i] + '/source_C.pt'
        print(modelpath)
        netC_list[i].load_state_dict(torch.load(modelpath))
        netC_list[i].eval()
        for k, v in netC_list[i].named_parameters():
            v.requires_grad = False

    for k, v in netQ.named_parameters():
        param_group += [{'params': v, 'lr': args.lr}]

    optimizer = optim.SGD(param_group)
    optimizer = op_copy(optimizer)

    max_iter = args.max_epoch * len(dset_loaders["target"])
    interval_iter = max_iter // args.interval
    iter_num = 0

    acc_init = 0

    while iter_num < max_iter:
        try:
            inputs_test, _, tar_idx = next(iter_test)
        except:
            iter_test = iter(dset_loaders["target"])
            inputs_test, _, tar_idx = next(iter_test)

        if inputs_test.size(0) == 1:
            continue

        if iter_num % interval_iter == 0 and args.cls_par > 0:

            for i in range(len(args.src)):
                netF_list[i].eval()
                netB_list[i].eval()
            netQ.eval()

            memory_label, _, _, _ = obtain_pseudo_label(dset_loaders['test'], netF_list, netB_list, netC_list, netQ,
                                                        args)
            memory_label = torch.from_numpy(memory_label).cuda() # memory_label是伪标签

            for i in range(len(args.src)):
                netF_list[i].train()
                netB_list[i].train()
            netQ.train()

        inputs_test = inputs_test.cuda() # 将数据转移到GPU上
        source_repre = torch.eye(len(args.src)).cuda() # source_repre是一个对角矩阵，nxn，n是源域的数量

        iter_num += 1
        lr_scheduler(optimizer, iter_num=iter_num, max_iter=max_iter)

        outputs_all = torch.zeros(len(args.src), inputs_test.shape[0], args.class_num) # outputs_all是一个三维张量，第一维是源域的数量，第二维是batch_size，第三维是类别数
        outputs_all_re = torch.zeros(len(args.src), inputs_test.shape[0], args.class_num) # outputs_all_re是一个三维张量，第一维是源域的数量，第二维是batch_size，第三维是类别数
        outputs_all_w = torch.zeros(inputs_test.shape[0], args.class_num) # outputs_all_w是一个二维张量，第一维是batch_size，第二维是类别数
        init_ent = torch.zeros(1, len(args.src)) # init_ent是一个二维张量，第一维是1，第二维是源域的数量, 初始化为0， 作为熵的损失

        for i in range(len(args.src)):
            features_test = netB_list[i](netF_list[i](inputs_test))
            outputs_test = netC_list[i](features_test)
            softmax_prob = nn.Softmax(dim=1)(outputs_test) # 计算softmax概率， batch_size * class_num

            ent_loss = torch.mean(loss.Entropy(softmax_prob)) # 计算熵的损失
            init_ent[:, i] = ent_loss
            outputs_all[i] = outputs_test

        source_weight = netQ(source_repre).unsqueeze(0).squeeze(2) # netQ用来计算权重
        weights_all = torch.repeat_interleave(source_weight, inputs_test.shape[0], dim=0).cpu() # 将权重重复batch_size次

        z = torch.sum(weights_all, dim=1) # 计算权重的和
        z = z + 1e-16 # 防止除0
        weights_all = torch.transpose(torch.transpose(weights_all, 0, 1) / z, 0, 1) # 归一化

        z = torch.sum(weights_all, dim=1) # 再次计算权重的和
        z = z + 1e-16 # 防止除0

        weights_all = torch.transpose(torch.transpose(weights_all, 0, 1) / z, 0, 1) # 再次归一化
        outputs_all = torch.transpose(outputs_all, 0, 1) # 转置为dim->batch_size * source_num * class_num

        for i in range(inputs_test.shape[0]):
            outputs_all_w[i] = torch.matmul(torch.transpose(outputs_all[i], 0, 1), weights_all[i]) # 计算加权后的输出, dim->batch_size * class_num

        weights_all = torch.transpose(weights_all, 0, 1) # 转置为dim->source_num * batch_size
        outputs_all = torch.transpose(outputs_all, 0, 1) # 转置为dim->source_num * batch_size * class_num
        for i in range(len(args.src)):
            weights_repeat = torch.repeat_interleave(weights_all[i].unsqueeze(1), args.class_num, dim=1)
            outputs_all_re[i] = outputs_all[i] * weights_repeat # 计算加权后的输出, dim->source_num * batch_size * class_num

        pred = memory_label[tar_idx].cpu().long() # tar_idx是目标域的索引， memory_label是伪标签
        if args.cls_par > 0:
            classifier_loss = args.cls_par * nn.CrossEntropyLoss()(outputs_all_w, pred)
        else:
            classifier_loss = torch.tensor(0.0)

        if args.crc_par > 0:
            consistency_loss = args.crc_par * loss.KLConsistencyLoss(outputs_all_re, pred, args)

        else:
            consistency_loss = torch.tensor(0.0)

        classifier_loss += consistency_loss

        if args.ent:
            softmax_out = nn.Softmax(dim=1)(outputs_all_w)
            entropy_loss = torch.mean(loss.Entropy(softmax_out))
            if args.gent:
                msoftmax = softmax_out.mean(dim=0)
                entropy_loss -= torch.sum(-msoftmax * torch.log(msoftmax + 1e-5))

            im_loss = entropy_loss * args.ent_par
            classifier_loss += im_loss

        optimizer.zero_grad()
        classifier_loss.backward()
        optimizer.step()

        if iter_num % interval_iter == 0 or iter_num == max_iter:
            for i in range(len(args.src)):
                netF_list[i].eval()
                netB_list[i].eval()
            netQ.eval()
            acc, _ = cal_acc_multi(dset_loaders['test'], netF_list, netB_list, netC_list, netQ, args)
            log_str = 'Iter:{}/{}; Accuracy = {:.2f}%'.format(iter_num, max_iter, acc)
            print(log_str + '\n')

            if acc >= acc_init:
                acc_init = acc

                for i in range(len(args.src)):
                    torch.save(netF_list[i].state_dict(),
                               osp.join(args.output_dir, "target_F_" + str(i) + "_" + args.savename + ".pt"))
                    torch.save(netB_list[i].state_dict(),
                               osp.join(args.output_dir, "target_B_" + str(i) + "_" + args.savename + ".pt"))
                    torch.save(netC_list[i].state_dict(),
                               osp.join(args.output_dir, "target_C_" + str(i) + "_" + args.savename + ".pt"))
                torch.save(netQ.state_dict(),
                           osp.join(args.output_dir, "target_Q" + "_" + args.savename + ".pt"))


def obtain_pseudo_label(loader, netF_list, netB_list, netC_list, netQ, args):
    start_test = True  # loader是测试数据集，这里是指定的webcam
    with torch.no_grad():
        iter_test = iter(loader)
        for _ in range(len(loader)):
            data = next(iter_test)
            inputs = data[0]
            labels = data[1]
            inputs = inputs.cuda()
            source_repre = torch.eye(len(args.src)).cuda()  # source_repre是一个对角矩阵, nxn，n是源域的数量

            # 不带w的是列表，包含了源域的数量个张量，每个张量的维度是batch_size x class_num
            # 带w的是一个张量，维度是batch_size x class_num，聚合了源域的信息得到的结果
            outputs_all = torch.zeros(len(args.src), inputs.shape[0],
                                      args.class_num)  # outputs_all是一个三维张量，第一维是源域的数量，第二维是batch_size，第三维是类别数
            outputs_all_w = torch.zeros(inputs.shape[0],
                                        args.class_num)  # b,31 outputs_all_w是一个二维张量，第一维是batch_size，第二维是类别数

            features_all = torch.zeros(len(args.src), inputs.shape[0],
                                       args.bottleneck)  # 2,b,256 features_all是一个三维张量，第一维是源域的数量，第二维是batch_size，第三维是bottleneck的维度
            features_all_w = torch.zeros(inputs.shape[0],
                                         args.bottleneck)  # b,256 features_all_w是一个二维张量，第一维是batch_size，第二维是bottleneck的维度

            features_all_F = torch.zeros(len(args.src), inputs.shape[0], netF_list[
                0].in_features)  # 2,b,2048 features_all_F是一个三维张量，第一维是源域的数量，第二维是batch_size，第三维是特征提取器的输出维度
            features_all_F_w = torch.zeros(inputs.shape[0], netF_list[
                0].in_features)  # b,2048 features_all_F_w是一个二维张量，第一维是batch_size，第二维是特征提取器的输出维度

            for i in range(len(args.src)):
                features_F = netF_list[i](inputs)
                features = netB_list[i](features_F)
                outputs = netC_list[i](features)
                outputs_all[i] = outputs
                features_all[i] = features
                features_all_F[i] = features_F

            source_weight = netQ(source_repre).unsqueeze(0).squeeze(2) # netQ用来计算权重
            weights_all = torch.repeat_interleave(source_weight, inputs.shape[0], dim=0).cpu()

            z = torch.sum(weights_all, dim=1)
            z = z + 1e-16
            weights_all = torch.transpose(torch.transpose(weights_all, 0, 1) / z, 0, 1)

            outputs_all = torch.transpose(outputs_all, 0, 1)
            features_all = torch.transpose(features_all, 0, 1)
            features_all_F = torch.transpose(features_all_F, 0, 1)

            for i in range(inputs.shape[0]):
                outputs_all_w[i] = torch.matmul(torch.transpose(outputs_all[i], 0, 1), weights_all[i])
                features_all_w[i] = torch.matmul(torch.transpose(features_all[i], 0, 1), weights_all[i])
                features_all_F_w[i] = torch.matmul(torch.transpose(features_all_F[i], 0, 1), weights_all[i])

            if start_test:
                all_output = outputs_all_w.float().cpu() # b*31
                all_feature = features_all_w.float().cpu() # b*256
                all_feature_F = features_all_F_w.float().cpu() # b*2048
                all_label = labels.float() # b*1
                start_test = False
            else:
                all_output = torch.cat((all_output, outputs_all_w.float().cpu()), 0)
                all_feature = torch.cat((all_feature, features_all_w.float().cpu()), 0)
                all_feature_F = torch.cat((all_feature_F, features_all_F_w.float().cpu()), 0)
                all_label = torch.cat((all_label, labels.float()), 0)
    # STEP1: 通过计算输出类的概率获取不置信的样本
    all_output = nn.Softmax(dim=1)(all_output)
    _, predict = torch.max(all_output, 1)
    accuracy = torch.sum(torch.squeeze(predict).float() == all_label).item() / float(all_label.size()[0])

    # Probability
    all_prob = all_output.float().cpu().numpy()
    prob_max_id = all_prob.argsort(axis=1)[:, -1] # 按照列排序，取最大值的索引
    prob_max2_id = all_prob.argsort(axis=1)[:, -2] # 按照列排序，取第二大值的索引

    prob_max = np.zeros(all_prob.shape[0])
    prob_max2 = np.zeros(all_prob.shape[0])
    for i in range(all_prob.shape[0]):
        prob_max[i] = all_prob[i, prob_max_id[i]]
        prob_max2[i] = all_prob[i, prob_max2_id[i]]
    prob_diff = prob_max - prob_max2 # 计算最大值和第二大值prob的差值

    prob_diff_tsr = torch.from_numpy(prob_diff).detach() # 将numpy数组转换为tensor
    idx_unconfi_prob = prob_diff_tsr.topk(int((all_prob.shape[0] * 0.5)), largest=False)[-1] # 取最小的50%的值
    idx_unconfi_list_prob = idx_unconfi_prob.cpu().numpy().tolist()
    # STEP2: 通过计算输出类的特征和类的代表特征的余弦相似度获取不置信的样本
    all_fea = torch.cat((all_feature, torch.ones(all_feature.size(0), 1)), 1) # 将特征和全1的列拼接，目的是为了计算余弦相似度
    all_fea = (all_fea.t() / torch.norm(all_fea, p=2, dim=1)).t() # 归一化。 num_sample*(feature_dim+1)
    all_fea = all_fea.float().cpu().numpy()

    K = all_output.size(1) # K是类别数
    aff = all_output.float().cpu().numpy() # 输出的每类的概率， num_sample*K
    initc = aff.transpose().dot(all_fea) # -> K*(feature_dim+1), 计算的结果是每类的特征的加权和，每个类的代表特征
    initc = initc / (1e-8 + aff.sum(axis=0)[:, None]) # 归一化

    dd = cdist(all_fea, initc, 'cosine') # 计算余弦相似度， 和每一类的代表特征的余弦相似度。得到的结果是num_sample*K
    pred_label = dd.argmin(axis=1) # 取最小值的索引，得到的是预测的类别, 距离需要越小越好
    acc = np.sum(pred_label == all_label.float().numpy()) / len(all_fea)

    # Distance measure
    dd_min_id = dd.argsort(axis=1)[:, 0] # 按照列排序，取最小值的索引， num_sample * 1
    dd_min2_id = dd.argsort(axis=1)[:, 1] # 按照列排序，取第二小值的索引, num_sample * 1

    dd_min = np.zeros(dd.shape[0]) # 最小值列表, num_sample * 1
    dd_min2 = np.zeros(dd.shape[0]) # 第二小值列表, num_sample * 1
    for i in range(dd.shape[0]):
        dd_min[i] = dd[i, dd_min_id[i]]
        dd_min2[i] = dd[i, dd_min2_id[i]]
    dd_diff = dd_min2 - dd_min # 计算最小值和第二小值的差值， num_sample * 1

    dd_diff_tsr = torch.from_numpy(dd_diff).detach() # 将numpy数组转换为tensor
    dd_t_confi = dd_diff_tsr.topk(int((dd.shape[0] * 0.5)), largest=True)[-1] # 取最大的50%的值， 最大值和次大值的差值越大，说明置信度越高
    dd_confi_list = dd_t_confi.cpu().numpy().tolist()
    dd_confi_list.sort()
    idx_confi = dd_confi_list

    idx_all_arr = np.zeros(shape=dd.shape[0], dtype=np.int64)
    idx_all_arr[idx_confi] = 1 # 置信度高的样本标记为1， 置信度低的样本标记为0
    idx_unconfi_arr = np.where(idx_all_arr == 0)
    idx_unconfi_list_dd = list(idx_unconfi_arr[0])

    idx_unconfi_list = list(set(idx_unconfi_list_dd).intersection(set(idx_unconfi_list_prob))) # 取通过两种方法都认为是不置信的样本
    label_confi = np.ones(all_prob.shape[0], dtype="int64") # 初始化标签，所有样本都是置信的
    label_confi[idx_unconfi_list] = 0 # 不置信的样本标记为0， 此处置信根据两个方法的结果来判断，第一个是通过输出类的概率，第二个是通过输出类的特征和类的代表特征的余弦相似度
    _, all_idx_nn, _ = nearest_confi_anchor(all_feature_F, all_feature_F, label_confi) # 通过余弦相似度计算最近的样本 all_idx_nn是最近的样本的索引

    ln = label_confi.shape[0] # 标签的数量
    gamma = 0.15 * np.random.randn(ln, 1) + 0.85 # 生成一个随机数，用于融合

    all_fea_nearest = all_fea[all_idx_nn] # 最近的样本的特征

    all_fea_fuse = gamma * all_fea + (1 - gamma) * all_fea_nearest # 融合特征，自己的特征和最近的样本的特征融合

    for round in range(1):
        aff = np.eye(K)[pred_label] # 生成一个对角矩阵，对角线上的值是预测的类别
        initc = aff.transpose().dot(all_fea_fuse) # 计算每类的特征的加权和
        initc = initc / (1e-8 + aff.sum(axis=0)[:, None]) # 归一化
        dd_fuse = cdist(all_fea_fuse, initc, args.distance) # 计算余弦相似度

        pred_label = dd_fuse.argmin(axis=1)
        acc = np.sum(pred_label == all_label.float().numpy()) / len(all_fea)

    log_str = 'Accuracy = {:.2f}% -> {:.2f}%'.format(accuracy * 100, acc * 100)
    print(log_str + '\n')

    return pred_label.astype('int'), all_feature_F, label_confi, all_label


def nearest_confi_anchor(data_q, data_all, lab_confi):
    data_q_ = data_q.detach() # 将tensor转换为numpy数组
    data_all_ = data_all.detach() # 将tensor转换为numpy数组
    data_q_ = data_q_.cpu().numpy() # 将tensor转换为numpy数组
    data_all_ = data_all_.cpu().numpy() # 将tensor转换为numpy数组
    num_sam = data_q.shape[0] # 样本数量
    LN_MEM = 70 # 最大的历史记录数

    flag_is_done = 0 # 是否完成的标志
    ctr_oper = 0 # 计数器， counter of operation
    idx_left = np.arange(0, num_sam, 1) # 未处理的样本索引
    mtx_mem_rlt = -3 * np.ones((num_sam, LN_MEM), dtype='int64') # 记录最近的样本索引
    mtx_mem_ignore = np.zeros((num_sam, LN_MEM), dtype='int64') # 记录忽略的样本索引
    is_mem = 0 # 是否有历史记录
    mtx_log = np.zeros((num_sam, LN_MEM), dtype='int64') # 记录标签
    indices_row = np.arange(0, num_sam, 1) # 样本索引
    nearest_idx_last = np.array([-7]) # 最近的样本索引

    while flag_is_done == 0:

        nearest_idx_tmp, idx_last_tmp = nearest_id_search(data_q_, data_all_, is_mem, ctr_oper, mtx_mem_ignore,
                                                          nearest_idx_last) # 查找最近的样本索引
        is_mem = 1
        nearest_idx_last = nearest_idx_tmp

        if ctr_oper == (LN_MEM - 1):
            flag_sw_bad = 1
        else:
            flag_sw_bad = 0

        mtx_mem_rlt[:, ctr_oper] = nearest_idx_tmp
        mtx_mem_ignore[:, ctr_oper] = idx_last_tmp

        lab_confi_tmp = lab_confi[nearest_idx_tmp]
        idx_done_tmp = np.where(lab_confi_tmp == 1)[0]
        idx_left[idx_done_tmp] = -1

        if flag_sw_bad == 1:
            idx_bad = np.where(idx_left >= 0)[0]
            mtx_log[idx_bad, 0] = 1
        else:
            mtx_log[:, ctr_oper] = lab_confi_tmp

        flag_len = len(np.where(idx_left >= 0)[0])

        if flag_len == 0 or flag_sw_bad == 1:
            idx_nn_step = []
            for k in range(num_sam):
                try:
                    idx_ts = list(mtx_log[k, :]).index(1)
                    idx_nn_step.append(idx_ts)
                except:
                    print("ts:", k, mtx_log[k, :])
                    idx_nn_step.append(0)

            idx_nn_re = mtx_mem_rlt[indices_row, idx_nn_step]
            data_re = data_all[idx_nn_re, :]
            flag_is_done = 1
        else:
            data_q_ = data_all_[nearest_idx_tmp, :]
        ctr_oper += 1

    return data_re, idx_nn_re, idx_nn_step


def nearest_id_search(Q, X, is_mem_f, step_num, mtx_ignore,
                      nearest_idx_last_f):
    Xt = np.transpose(X)
    Simo = np.dot(Q, Xt) # 得到的结果是Q和X的内积， 可以代表两个向量的相似度
    nq = np.expand_dims(LA.norm(Q, axis=1), axis=1) # 计算Q的范数
    nx = np.expand_dims(LA.norm(X, axis=1), axis=0) # 计算X的范数
    Nor = np.dot(nq, nx) # 计算Q和X的范数的乘积
    Sim = 1 - (Simo / Nor) # 计算余弦相似度，dim: Q.shape[0] * X.shape[0]

    indices_min = np.argmin(Sim, axis=1) # 取最小值的索引
    indices_row = np.arange(0, Q.shape[0], 1) #

    idx_change = np.where((indices_min - nearest_idx_last_f) != 0)[0]
    if is_mem_f == 1:
        if idx_change.shape[0] != 0:
            indices_min[idx_change] = nearest_idx_last_f[idx_change]
    Sim[indices_row, indices_min] = 1000 #

    # Ignore the history search records.
    if is_mem_f == 1:
        for k in range(step_num):
            indices_ingore = mtx_ignore[:, k]
            Sim[indices_row, indices_ingore] = 1000

    indices_min_cur = np.argmin(Sim, axis=1)
    indices_self = indices_min
    return indices_min_cur, indices_self


def cal_acc_multi(loader, netF_list, netB_list, netC_list, netQ, args):
    start_test = True
    with torch.no_grad():
        iter_test = iter(loader)
        for _ in range(len(loader)):
            data = next(iter_test)
            inputs = data[0]
            labels = data[1]
            inputs = inputs.cuda()
            source_repre = torch.eye(len(args.src)).cuda()

            outputs_all = torch.zeros(len(args.src), inputs.shape[0], args.class_num)
            outputs_all_w = torch.zeros(inputs.shape[0], args.class_num)

            for i in range(len(args.src)):
                features = netB_list[i](netF_list[i](inputs))
                outputs = netC_list[i](features)
                outputs_all[i] = outputs

            source_weight = netQ(source_repre).unsqueeze(0).squeeze(2)
            weights_all = torch.repeat_interleave(source_weight, inputs.shape[0], dim=0).cpu()

            z = torch.sum(weights_all, dim=1)
            z = z + 1e-16
            weights_all = torch.transpose(torch.transpose(weights_all, 0, 1) / z, 0, 1)
            outputs_all = torch.transpose(outputs_all, 0, 1)

            for i in range(inputs.shape[0]):
                outputs_all_w[i] = torch.matmul(torch.transpose(outputs_all[i], 0, 1), weights_all[i])

            if start_test:
                all_output = outputs_all_w.float().cpu()
                all_label = labels.float()
                start_test = False
            else:
                all_output = torch.cat((all_output, outputs_all_w.float().cpu()), 0)
                all_label = torch.cat((all_label, labels.float()), 0)
    _, predict = torch.max(all_output, 1)
    accuracy = torch.sum(torch.squeeze(predict).float() == all_label).item() / float(all_label.size()[0])
    mean_ent = torch.mean(loss.Entropy(nn.Softmax(dim=1)(all_output))).cpu().data.item()
    return accuracy * 100, mean_ent


def print_args(args):
    s = "==========================================\n"
    for arg, content in args.__dict__.items():
        s += "{}:{}\n".format(arg, content)
    return s


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='CAiDA')
    parser.add_argument('--gpu_id', type=str, nargs='?', default='2', help="device id to run")
    parser.add_argument('--t', type=int, default=0,
                        help="target")  ## Choose which domain to set as target {0 to len(names)-1}
    parser.add_argument('--max_epoch', type=int, default=15, help="max iterations")
    parser.add_argument('--interval', type=int, default=15)
    parser.add_argument('--batch_size', type=int, default=32, help="batch_size")
    parser.add_argument('--worker', type=int, default=4, help="number of workers")
    parser.add_argument('--dset', type=str, default='office-31', choices=['office-31', 'office-home', 'office-caltech'])
    parser.add_argument('--lr', type=float, default=1 * 1e-2, help="learning rate")
    parser.add_argument('--net', type=str, default='resnet50', help="vgg16, resnet50, res101")
    parser.add_argument('--seed', type=int, default=2022, help="random seed")

    parser.add_argument('--gent', type=bool, default=True)
    parser.add_argument('--ent', type=bool, default=True)
    parser.add_argument('--threshold', type=int, default=0)
    parser.add_argument('--cls_par', type=float, default=0.7)
    parser.add_argument('--ent_par', type=float, default=1.0)
    parser.add_argument('--crc_par', type=float, default=1e-2)
    parser.add_argument('--lr_decay1', type=float, default=0.1)
    parser.add_argument('--lr_decay2', type=float, default=1.0)

    parser.add_argument('--bottleneck', type=int, default=256)
    parser.add_argument('--epsilon', type=float, default=1e-5)
    parser.add_argument('--layer', type=str, default="wn", choices=["linear", "wn"])
    parser.add_argument('--classifier', type=str, default="bn", choices=["ori", "bn"])
    parser.add_argument('--distance', type=str, default='cosine', choices=["euclidean", "cosine"])
    parser.add_argument('--output', type=str, default='ckps/MSFDA')
    parser.add_argument('--output_src', type=str, default='ckps/source')
    args = parser.parse_args()

    if args.dset == 'office-home':
        names = ['Art', 'Clipart', 'Product', 'Real_World']
        args.class_num = 65
    elif args.dset == 'office-31':
        names = ['amazon', 'dslr', 'webcam']
        args.class_num = 31
    elif args.dset == 'office-caltech':
        names = ['amazon', 'caltech', 'dslr', 'webcam']
        args.class_num = 10
    else:
        raise ValueError('Dataset cannot be recognized. Please define your own dataset here.')

    args.src = []
    for i in range(len(names)):
        if i == args.t:
            continue
        else:
            args.src.append(names[i])

    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu_id
    SEED = args.seed
    torch.manual_seed(SEED)
    torch.cuda.manual_seed(SEED)
    np.random.seed(SEED)
    random.seed(SEED)

    for i in range(len(names)):
        if i != args.t:
            continue

        folder = 'data'
        # args.t_dset_path = folder + args.dset + '/' + names[args.t] + '_list.txt'
        args.t_dset_path = osp.join(folder, args.dset, names[args.t] + '_list.txt')
        # args.test_dset_path = folder + args.dset + '/' + names[args.t] + '_list.txt'
        args.test_dset_path = osp.join(folder, args.dset, names[args.t] + '_list.txt')
        print(args.t_dset_path)

    args.output_dir_src = []
    for i in range(len(args.src)):
        args.output_dir_src.append(osp.join(args.output_src, args.dset, args.src[i][0].upper()))
    print(args.output_dir_src)
    args.output_dir = osp.join(args.output, args.dset, names[args.t][0].upper())

    # if not osp.exists(args.output_dir):
    #     os.system('mkdir -p ' + args.output_dir)
    if not osp.exists(args.output_dir):
        os.mkdir(args.output_dir)

    args.savename = 'par_' + str(args.cls_par) + '_' + str(args.crc_par)

    train_target(args)
