import math
from functools import partial

import numpy as np
import torch
import torch.nn as nn

class YOLOLoss(nn.Module):
    def __init__(self, anchors, num_classes, input_shape, cuda, anchors_mask = [[6,7,8], [3,4,5], [0,1,2]]) -> None:
        super(YOLOLoss, self).__init__()
        self.anchors        = anchors
        self.num_classes    = num_classes
        self.bbox_attrs     = 5 + num_classes
        self.input_shape    = input_shape
        self.anchors_mask   = anchors_mask

        self.ignore_threshold = 0.5
        self.cuda           = cuda
    
    def clip_by_tensor(self, t, t_min, t_max):
        t = t.float()
        result = (t >= t_min).float() * t + (t < t_min).float() * t_min
        result = (result <= t_max).float() * result + (result > t_max).float() * t_max
        return result

    def BCELoss(self, pred, target):
        epsilon = 1e-7
        pred    = self.clip_by_tensor(pred, epsilon, 1.0 - epsilon)
        output  = - target * torch.log(pred) - (1.0 - target) * torch.log(1.0 - pred)
        return output
    
    def forward(self, l, input, targets=None):
        #l代表的是，当前输入进来的有效特征层，是第几个有效特征层
        #input的shape为  bs, 3*(5+num_classes), 13, 13
        #                bs, 3*(5+num_classes), 26, 26
        #                bs, 3*(5+num_classes), 52, 52
        #targets代表的是真实框。

        #获得图片数量，特征层的高和宽
        #13和13
        bs      = input.size(0)
        in_h    = input.size(2)
        in_w    = input.size(3)
        #计算步长
        stride_h = self.input_shape[0] / in_h
        stride_w = self.input_shape[1] / in_w
        #scaled_anchors大小是相对于特征层的
        scaled_anchors  = [(a_w / stride_w, a_h / stride_h) for a_w, a_h in self.anchors]

        #输入的input一共有三个，他们的shape分别是
        #bs, 3*(5+num_classes), 13, 13 => batch_size, 3, 13, 13, 5 + num_classes
        #batch_size, 3, 26, 26, 5 + num_classes
        #batch_size, 3, 52, 52, 5 + num_classes
        prediction = input.view(bs, len(self.anchors_mask[l]), self.bbox_attrs, in_h, in_w).permute(0, 1, 3, 4, 2).contiguous()
        #先验框的中心位置的调整参数
        x = torch.sigmoid(prediction[..., 0])
        y = torch.sigmoid(prediction[..., 1])
        #先验框的宽高调整参数
        w = prediction[..., 2]
        h = prediction[..., 3]
        #获得置信度，是否有物体
        conf = torch.sigmoid(prediction[..., 4])
        #种类置信度
        pred_cls = torch.sigmoid(prediction[..., 5:])
        #获得网络应该有的预测结果
        y_true, noobj_mask, box_loss_scale = self.get_target(l, targets, scaled_anchors, in_h, in_w)

        #将预测结果进行解码，判断预测结果和真实值的重合程度
        #如果重合程度过大则忽略，因为这些特征点属于预测比较准确的特征点
        #作为负样本不合适
        noobj_mask, pred_boxes = self.get_ignore(l, x, y, h, w, targets, scaled_anchors, in_h, in_w, noobj_mask)

        if self.cuda:
            y_true = y_true.type_as(x)
            noobj_mask = noobj_mask.type_as(x)
            box_loss_scale = box_loss_scale.type_as(x)
        #box_loss_scale是真实框宽高的乘积，宽高均在0-1之间，因此乘积也在0-1之间。
        #2-宽高的乘积代表真实框越大，比重越小，小框的比重更大。
        box_loss_scale = 2 - box_loss_scale

        #计算中心偏移情况的loss
        loss_x = torch.sum(self.BCELoss(x, y_true[..., 0]) * box_loss_scale * y_true[..., 4])
        loss_y = torch.sum(self.BCELoss(y, y_true[..., 1]) * box_loss_scale * y_true[..., 4])
        #计算宽高调整值的loss
        loss_w = torch.sum(self.BCELoss(w, y_true[..., 2]) * 0.5 * box_loss_scale * y_true[..., 4])
        loss_h = torch.sum(self.BCELoss(h, y_true[..., 3]) * 0.5 * box_loss_scale * y_true[..., 4])
        
        #计算置信度的loss
        loss_conf   = torch.sum(self.BCELoss(conf, y_true[..., 4]) * y_true[..., 4]) + \
                      torch.sum(self.BCELoss(conf, y_true[..., 4])* noobj_mask)
        loss_cls    = torch.sum(self.BCELoss(pred_cls[y_true[..., 4] == 1], y_true[..., 5:][y_true[..., 4] == 1]))
        loss = loss_cls + loss_x + loss_y + loss_h + loss_w + loss_conf
        num_pos = torch.sum(y_true[..., 4])
        num_pos = torch.max(num_pos, torch.ones_like(num_pos))
        return loss, num_pos

    def calculate_iou(self, _box_a, _box_b):
        b1_x1, b1_x2 = _box_a[:, 0] - _box_a[:, 2] / 2, _box_a[:, 0] + _box_a[:, 2] / 2
        b1_y1, b1_y2 = _box_a[:, 1] - _box_a[:, 3] / 2, _box_a[:, 1] + _box_a[:, 3] / 2

        b2_x1, b2_x2 = _box_b[:, 0] - _box_b[:, 2] / 2, _box_b[:, 0] + _box_b[:, 2] / 2
        b2_y1, b2_y2 = _box_b[:, 1] - _box_b[:, 3] / 2, _box_b[:, 1] + _box_b[:, 3] / 2

        box_a = torch.zeros_like(_box_a)
        box_b = torch.zeros_like(_box_b)
        box_a[:, 0], box_a[:, 1], box_a[:, 2], box_a[:, 3] = b1_x1, b1_y1, b1_x2, b1_y2
        box_b[:, 0], box_b[:, 1], box_b[:, 2], box_b[:, 3] = b2_x1, b2_y1, b2_x2, b2_y2

        #A为真实框的数量，B为先验框的数量
        A = box_a.size(0)
        B = box_b.size(0)

        max_xy  = torch.min(box_a[:, 2:].unsqueeze(1).expand(A, B, 2), box_b[:, 2:].unsqueeze(0).expand(A, B, 2))
        min_xy  = torch.max(box_a[:, :2].unsqueeze(1).expand(A, B, 2), box_b[:, :2].unsqueeze(0).expand(A, B, 2))
        inter   = torch.clamp((max_xy - min_xy), min=0)
        inter   = inter[:, :, 0] * inter[:, :, 1]
        area_a = ((box_a[:, 2]-box_a[:, 0]) * (box_a[:, 3]-box_a[:, 1])).unsqueeze(1).expand_as(inter)  # [A,B]
        area_b = ((box_b[:, 2]-box_b[:, 0]) * (box_b[:, 3]-box_b[:, 1])).unsqueeze(0).expand_as(inter)  # [A,B]
        #求IOU
        union = area_a + area_b - inter
        return inter / union  # [A,B]

    def get_target(self, l, targets, anchors, in_h, in_w):
        #计算一共有多少张图片
        bs              = len(targets)
        #用于选取哪些先验框不包含物体
        noobj_mask      = torch.ones(bs, len(self.anchors_mask[l]), in_h, in_w, requires_grad = False)
        box_loss_scale  = torch.zeros(bs, len(self.anchors_mask[l]), in_h, in_w, requires_grad = False)
        y_true          = torch.zeros(bs, len(self.anchors_mask[l]), in_h, in_w, self.bbox_attrs, requires_grad = False)
        for b in range(bs):            
            if len(targets[b])==0:
                continue
            batch_target = torch.zeros_like(targets[b])
            #计算出正样本在特征层上的中心点
            batch_target[:, [0,2]] = targets[b][:, [0,2]] * in_w
            batch_target[:, [1,3]] = targets[b][:, [1,3]] * in_h
            batch_target[:, 4] = targets[b][:, 4]
            batch_target = batch_target.cpu()
            
            #将真实框转换一个形式
            gt_box          = torch.FloatTensor(torch.cat((torch.zeros((batch_target.size(0), 2)), batch_target[:, 2:4]), 1))
            #将先验框转换一个形式
            anchor_shapes   = torch.FloatTensor(torch.cat((torch.zeros((len(anchors), 2)), torch.FloatTensor(anchors)), 1))
            #   best_ns:每个真实框最大的重合度max_iou, 每一个真实框最重合的先验框的序号
            best_ns = torch.argmax(self.calculate_iou(gt_box, anchor_shapes), dim=-1)

            for t, best_n in enumerate(best_ns):
                if best_n not in self.anchors_mask[l]:
                    continue
                #判断这个先验框是当前特征点的哪一个先验框
                k = self.anchors_mask[l].index(best_n)
                #获得真实框属于哪个网格点
                i = torch.floor(batch_target[t, 0]).long()
                j = torch.floor(batch_target[t, 1]).long()
                #取出真实框的种类
                c = batch_target[t, 4].long()

                #noobj_mask代表无目标的特征点
                noobj_mask[b, k, j, i] = 0
                #tx、ty代表中心调整参数的真实值
                y_true[b, k, j, i, 0] = batch_target[t, 0] - i.float()
                y_true[b, k, j, i, 1] = batch_target[t, 1] - j.float()
                y_true[b, k, j, i, 2] = math.log(batch_target[t, 2] / anchors[best_n][0])
                y_true[b, k, j, i, 3] = math.log(batch_target[t, 3] / anchors[best_n][1])
                y_true[b, k, j, i, 4] = 1
                y_true[b, k, j, i, c + 5] = 1
                #用于获得xywh的比例
                #大目标loss权重小，小目标loss权重大
                box_loss_scale[b, k, j, i] = batch_target[t, 2] * batch_target[t, 3] / in_w / in_h
        return y_true, noobj_mask, box_loss_scale

    def get_ignore(self, l, x, y, h, w, targets, scaled_anchors, in_h, in_w, noobj_mask):
        #计算一共有多少张图片
        bs = len(targets)
        #生成网格，先验框中心，网格左上角
        grid_x = torch.linspace(0, in_w - 1, in_w).repeat(in_h, 1).repeat(
            int(bs * len(self.anchors_mask[l])), 1, 1).view(x.shape).type_as(x)
        grid_y = torch.linspace(0, in_h - 1, in_h).repeat(in_w, 1).t().repeat(
            int(bs * len(self.anchors_mask[l])), 1, 1).view(y.shape).type_as(x)
        #生成先验框的宽高
        scaled_anchors_l = np.array(scaled_anchors)[self.anchors_mask[l]]
        anchor_w = torch.Tensor(scaled_anchors_l).index_select(1, torch.LongTensor([0])).type_as(x)
        anchor_h = torch.Tensor(scaled_anchors_l).index_select(1, torch.LongTensor([1])).type_as(x)
        
        anchor_w = anchor_w.repeat(bs, 1).repeat(1, 1, in_h * in_w).view(w.shape)
        anchor_h = anchor_h.repeat(bs, 1).repeat(1, 1, in_h * in_w).view(h.shape)
        #计算调整后的先验框中心与宽高
        pred_boxes_x    = torch.unsqueeze(x + grid_x, -1)
        pred_boxes_y    = torch.unsqueeze(y + grid_y, -1)
        pred_boxes_w    = torch.unsqueeze(torch.exp(w) * anchor_w, -1)
        pred_boxes_h    = torch.unsqueeze(torch.exp(h) * anchor_h, -1)
        pred_boxes      = torch.cat([pred_boxes_x, pred_boxes_y, pred_boxes_w, pred_boxes_h], dim = -1)
        
        for b in range(bs):           
            #将预测结果转换一个形式
            pred_boxes_for_ignore = pred_boxes[b].view(-1, 4)
            #计算真实框，并把真实框转换成相对于特征层的大小
            if len(targets[b]) > 0:
                batch_target = torch.zeros_like(targets[b])
                #计算出正样本在特征层上的中心点
                batch_target[:, [0,2]] = targets[b][:, [0,2]] * in_w
                batch_target[:, [1,3]] = targets[b][:, [1,3]] * in_h
                batch_target = batch_target[:, :4].type_as(x)
                #计算IoU
                anch_ious = self.calculate_iou(batch_target, pred_boxes_for_ignore)
                anch_ious_max, _    = torch.max(anch_ious, dim = 0)
                anch_ious_max       = anch_ious_max.view(pred_boxes[b].size()[:3])
                noobj_mask[b][anch_ious_max > self.ignore_threshold] = 0
        return noobj_mask, pred_boxes

def weights_init(net, init_type='normal', init_gain = 0.02):
    def init_func(m):
        classname = m.__class__.__name__
        if hasattr(m, 'weight') and classname.find('Conv') != -1:
            if init_type == 'normal':
                torch.nn.init.normal_(m.weight.data, 0.0, init_gain)
            elif init_type == 'xavier':
                torch.nn.init.xavier_normal_(m.weight.data, gain=init_gain)
            elif init_type == 'kaiming':
                torch.nn.init.kaiming_normal_(m.weight.data, a=0, mode='fan_in')
            elif init_type == 'orthogonal':
                torch.nn.init.orthogonal_(m.weight.data, gain=init_gain)
            else:
                raise NotImplementedError('initialization method [%s] is not implemented' % init_type)
        elif classname.find('BatchNorm2d') != -1:
            torch.nn.init.normal_(m.weight.data, 1.0, 0.02)
            torch.nn.init.constant_(m.bias.data, 0.0)
    print('initialize network with %s type' % init_type)
    net.apply(init_func)

def get_lr_scheduler(lr_decay_type, lr, min_lr, total_iters, warmup_iters_ratio = 0.05, warmup_lr_ratio = 0.1, no_aug_iter_ratio = 0.05, step_num = 10):
    def yolox_warm_cos_lr(lr, min_lr, total_iters, warmup_total_iters, warmup_lr_start, no_aug_iter, iters):
        if iters <= warmup_total_iters:
            # lr = (lr - warmup_lr_start) * iters / float(warmup_total_iters) + warmup_lr_start
            lr = (lr - warmup_lr_start) * pow(iters / float(warmup_total_iters), 2) + warmup_lr_start
        elif iters >= total_iters - no_aug_iter:
            lr = min_lr
        else:
            lr = min_lr + 0.5 * (lr - min_lr) * (
                1.0 + math.cos(math.pi* (iters - warmup_total_iters) / (total_iters - warmup_total_iters - no_aug_iter))
            )
        return lr

    def step_lr(lr, decay_rate, step_size, iters):
        if step_size < 1:
            raise ValueError("step_size must above 1.")
        n       = iters // step_size
        out_lr  = lr * decay_rate ** n
        return out_lr

    if lr_decay_type == "cos":
        warmup_total_iters  = min(max(warmup_iters_ratio * total_iters, 1), 3)
        warmup_lr_start     = max(warmup_lr_ratio * lr, 1e-6)
        no_aug_iter         = min(max(no_aug_iter_ratio * total_iters, 1), 15)
        func = partial(yolox_warm_cos_lr ,lr, min_lr, total_iters, warmup_total_iters, warmup_lr_start, no_aug_iter)
    else:
        decay_rate  = (min_lr / lr) ** (1 / (step_num - 1))
        step_size   = total_iters / step_num
        func = partial(step_lr, lr, decay_rate, step_size)

    return func

def set_optimizer_lr(optimizer, lr_scheduler_func, epoch):
    lr = lr_scheduler_func(epoch)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr