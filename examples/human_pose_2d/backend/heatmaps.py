import numpy as np
import tensorflow as tf
from Munkres import Munkres


def non_maximum_supressions(heatmaps):
    heatmaps = np.transpose(heatmaps, [0, 2, 3, 1])
    maximum_values = tf.keras.layers.MaxPooling2D(pool_size=3, strides=1,
                                                  padding='same')(heatmaps)
    maximum_values = np.equal(maximum_values, heatmaps)
    maximum_values = maximum_values.astype(np.float32)
    filtered_heatmaps = heatmaps * maximum_values
    return filtered_heatmaps


def unpack_heatmaps_dimensions(heatmaps):
    num_images, joints_count, H, W = heatmaps.shape[:4]
    return num_images, joints_count, H, W


def torch_gather(tags, indices, gather_axis=2):
    tags = tags.astype(np.int64)
    indices = indices.astype(np.int64)
    all_indices = np.ndarray(indices.shape)
    all_indices.fill(True)
    all_indices = tf.where(all_indices)
    gather_locations = indices.flatten()
    gather_indices = []
    for axis in range(len(indices.shape)):
        if axis == gather_axis:
            gather_indices.append(gather_locations)
        else:
            gather_indices.append(all_indices[:, axis])

    gather_indices = np.stack(gather_indices, axis=-1)
    gathered = tf.gather_nd(tags, gather_indices)
    return np.reshape(gathered, indices.shape)


def get_top_k_heatmaps(heatmaps, max_num_people):
    top_k_heatmaps, indices = tf.math.top_k(heatmaps, max_num_people)
    return np.squeeze(top_k_heatmaps), tensor_to_numpy(indices)


def get_top_k_tags(tags, indices):
    top_k_tags = []
    for arg in range(tags.shape[3]):
        top_k_tags.append(torch_gather(tags[:, :, :, 0], indices))
    top_k_tags = np.stack(top_k_tags, axis=3)
    return np.squeeze(top_k_tags)


def get_top_k_locations(indices, image_width):
    x = (indices % image_width).astype(np.int64)
    y = (indices / image_width).astype(np.int64)
    top_k_locations = np.stack((x, y), axis=3)
    return np.squeeze(top_k_locations)


def top_k_detections(heatmaps, tags, max_num_people):
    heatmaps = non_maximum_supressions(heatmaps)
    heatmaps = np.transpose(heatmaps, [0, 3, 1, 2])
    num_images, joints_count, H, W = unpack_heatmaps_dimensions(heatmaps)
    heatmaps = np.reshape(heatmaps, [num_images, joints_count, -1])
    tags = np.reshape(tags, [tags.shape[0], tags.shape[1], W*H, -1])

    top_k_heatmaps, indices = get_top_k_heatmaps(heatmaps, max_num_people)
    top_k_tags = get_top_k_tags(tags, indices)
    top_k_locations = get_top_k_locations(indices, W)

    top_k_detections = {'top_k_tags': top_k_tags,
                        'top_k_locations': top_k_locations,
                        'top_k_heatmaps': top_k_heatmaps
                        }
    return top_k_detections


def update_dictionary(tags, joints, arg, default, joint_dict, tag_dict):
    for tag, joint in zip(tags, joints):
        key = tag[0]
        joint_dict.setdefault(key, np.copy(default))[arg] = joint
        tag_dict[key] = [tag]


def group_keys_and_tags(joint_dict, tag_dict, max_num_people):
    grouped_keys = list(joint_dict.keys())[:max_num_people]
    grouped_tags = [np.mean(tag_dict[arg], axis=0) for arg in grouped_keys]
    return grouped_keys, grouped_tags


def calculate_norm(joints, grouped_tags, order=2):
    difference = joints[:, None, 3:] - np.array(grouped_tags)[None, :, :]
    norm = np.linalg.norm(difference, ord=order, axis=2)
    num_added, num_grouped = difference.shape[:2]
    return norm, num_added, num_grouped


def update_norm(norm, num_added, num_grouped):
    shape = (num_added, (num_added - num_grouped))
    updated_norm = concatenate_zeros(norm, shape)
    return updated_norm


def round_norm(norm, valid_joints):
    norm = np.round(norm) * 100 - valid_joints[:, 2:3]
    return norm


def concatenate_zeros(metrix, shape):
    concatenated = np.concatenate((metrix, np.zeros(shape)+1e10), axis=1)
    return concatenated


def shortest_L2_distance(cost):
    munkres = Munkres(cost)
    lowest_cost_pairs = munkres.compute()
    lowest_cost_pairs = np.array(lowest_cost_pairs).astype(np.int32)
    return lowest_cost_pairs


def get_valid_tags_and_joints(detections, joint_arg, detection_thresh):
    tags, locations, heatmaps_values = detections.values()
    joints = np.concatenate((locations[joint_arg],
                             heatmaps_values[joint_arg, :, None],
                             tags[joint_arg]), 1)
    mask = joints[:, 2] > detection_thresh
    tags = tags[joint_arg]
    valid_tags = tags[mask]
    valid_joints = joints[mask]
    return valid_tags, valid_joints


def extract_grouped_joints(joint_dict):
    grouped_joints = []
    for joint_arg in joint_dict:
        grouped_joints.append(joint_dict[joint_arg])
    grouped_joints = np.array(grouped_joints).astype(np.float32)
    return [grouped_joints]


def group_joints_by_tag(detections, max_num_people, joint_order,
                        detection_thresh, tag_thresh):
    tags = detections['top_k_tags']
    joint_dict, tag_dict = {}, {}
    default = np.zeros((len(joint_order), tags.shape[2] + 3))

    for arg, joint_arg in enumerate(joint_order):
        tags, joints = get_valid_tags_and_joints(
            detections, joint_arg, detection_thresh)

        if joints.shape[0] == 0:
            continue
        if arg == 0 or len(joint_dict) == 0:
            update_dictionary(tags, joints, joint_arg, default, joint_dict,
                              tag_dict)
        else:
            grouped_keys, grouped_tags = group_keys_and_tags(
                joint_dict, tag_dict, max_num_people)
            norm, num_added, num_grouped = calculate_norm(joints, grouped_tags)
            norm_copy = np.copy(norm)
            norm = round_norm(norm, joints)
            if num_added > num_grouped:
                norm = update_norm(norm, num_added, num_grouped)

            lowest_cost_pairs = shortest_L2_distance(norm)
            for row_arg, col_arg in lowest_cost_pairs:
                if (row_arg < num_added and col_arg < num_grouped
                        and norm_copy[row_arg][col_arg] < tag_thresh):
                    key = grouped_keys[col_arg]
                    joint_dict[key][joint_arg] = joints[row_arg]
                    tag_dict[key].append(tags[row_arg])
                else:
                    update_dictionary(tags, joints, joint_arg, default,
                                      joint_dict, tag_dict)
    grouped_joints = extract_grouped_joints(joint_dict)
    return grouped_joints


def compare_vertical_neighbours(x, y, heatmap_value, offset=0.25):
    int_x, int_y = int(x), int(y)
    lower_y = min(int_y + 1, heatmap_value.shape[1] - 1)
    upper_y = max(int_y - 1, 0)
    if heatmap_value[int_x, lower_y] > heatmap_value[int_x, upper_y]:
        y = y + offset
    else:
        y = y - offset
    return y


def compare_horizontal_neighbours(x, y, heatmap_value, offset=0.25):
    int_x, int_y = int(x), int(y)
    left_x = max(0, int_x - 1)
    right_x = min(int_x + 1, heatmap_value.shape[0] - 1)
    if heatmap_value[right_x, int_y] > heatmap_value[left_x, int_y]:
        x = x + offset
    else:
        x = x - offset
    return x


def shift_joint_location(joint_location, offset=0):
    y, x = joint_location
    y = y + offset
    x = x + offset
    return y, x


def adjust_joints_locations(heatmaps, grouped_joints):
    for batch_id, people in enumerate(grouped_joints):
        for person_id, person in enumerate(people):
            for joint_id, joint in enumerate(person):
                heatmap = heatmaps[batch_id][joint_id]
                if joint[2] > 0:
                    y, x = joint[0:2]
                    y = compare_vertical_neighbours(x, y, heatmap)
                    x = compare_horizontal_neighbours(x, y, heatmap)
                    grouped_joints[batch_id][person_id, joint_id, 0:2] = \
                        shift_joint_location((y, x), offset=0.5)
    return grouped_joints


def calculate_tags_mean(joints, tags):
    if len(tags.shape) == 3:
        tags = tags[:, :, :, None]
    joints_tags = []
    for arg in range(joints.shape[0]):
        if joints[arg, 2] > 0:
            x, y = joints[arg][:2].astype(np.int32)
            joints_tags.append(tags[arg, y, x])
    tags_mean = np.mean(joints_tags, axis=0)
    return tags, tags_mean


def normalize_heatmap(arg, tags, tags_mean, heatmap):
    normalized_tags = (tags[arg, :, :] - tags_mean[None, None, :])
    normalized_tags_squared_sum = (normalized_tags ** 2).sum(axis=2)
    return heatmap - np.round(np.sqrt(normalized_tags_squared_sum))


def find_max_position(heatmap_per_joint, normalized_heatmap_per_joint):
    max_indices = np.argmax(normalized_heatmap_per_joint)
    shape = heatmap_per_joint.shape
    x, y = np.unravel_index(max_indices, shape)
    return x, y


def update_joints(joints, updated_joints, heatmaps):
    updated_joints = np.array(updated_joints)
    for i in range(heatmaps.shape[0]):
        if updated_joints[i, 2] > 0 and joints[i, 2] == 0:
            joints[i, :3] = updated_joints[i, :3]
    return joints


def refine_joints_locations(heatmaps, tags, joints_per_person):
    tags, tags_mean = calculate_tags_mean(joints_per_person, tags)
    updated_joints = []
    for arg in range(joints_per_person.shape[0]):
        heatmap_per_joint = heatmaps[arg, :, :]
        normalized_heatmap_per_joint = normalize_heatmap(arg, tags, tags_mean,
                                                         heatmap_per_joint)

        x, y = find_max_position(heatmap_per_joint,
                                 normalized_heatmap_per_joint)
        max_heatmaps_value = heatmap_per_joint[x, y]
        x, y = shift_joint_location((x, y), offset=0.5)
        y = compare_vertical_neighbours(x, y, heatmap_per_joint)
        x = compare_horizontal_neighbours(x, y, heatmap_per_joint)
        updated_joints.append((y, x, max_heatmaps_value))

    joints_per_person = update_joints(joints_per_person,
                                      updated_joints, heatmaps)
    return joints_per_person


def get_score(grouped_joints):
    score = []
    for joint in grouped_joints:
        score.append(joint[:, 2].mean())
    return score


def tensor_to_numpy(tensor):
    return tensor.cpu().numpy()
