import os
import gdown
import errno
import logging
from copy import deepcopy

import cv2
import numpy as np
import onnxruntime
from copy import deepcopy

logging.basicConfig(level=logging.DEBUG)


class OnnxSAM:
    """Segmentation model using SegmentAnything"""

    def __init__(self) -> None:
        self.target_size = 1024
        self.input_size = (684, 1024)


        #more optioins shortly
        self._model_urls = {"sam_vit_l_0b3195.encoder.quant.onnx": "https://drive.google.com/file/d/1cxFVincc2-Yi1WACbC46l9vF4rJoilq5/view?usp=drive_link",
                            "sam_vit_l_0b3195.decoder.quant.onnx": "https://drive.google.com/file/d/1xIx_jKftt8P4HEzNyKlm3S2Z-9pPdu9C/view?usp=drive_link"}

        self.__setup("sam_vit_l_0b3195.encoder.quant.onnx",
            "sam_vit_l_0b3195.decoder.quant.onnx"
        )


    def __load_model(self, path: str):
        try:
            if os.path.exists(path):
                # `providers` need to be set explicitly since ORT 1.9
                return onnxruntime.InferenceSession(
                    path, providers=onnxruntime.get_available_providers()
                )
            else:
                raise FileNotFoundError(
                    errno.ENOENT,
                    os.strerror(errno.ENOENT),
                    path,
                )
        except FileNotFoundError:

            data = f'{self.base_dir}/data'
            if not os.path.isdir(data):
                os.makedirs(data)


            gdown.download(url=self._model_urls[os.path.basename(path)], 
                            output=path, 
                            fuzzy=True
                            )
        
            # `providers` need to be set explicitly since ORT 1.9
            return onnxruntime.InferenceSession(
                path, providers=onnxruntime.get_available_providers()
            )
    

    def __setup(self, encoder_model_path, decoder_model_path):

        providers = onnxruntime.get_available_providers()
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        parse_path = lambda x: os.path.join(self.base_dir, "data", x)

        if providers:
            logging.info(
                "Available providers for ONNXRuntime: %s", ", ".join(providers)
            )
        else:
            logging.warning("No available providers for ONNXRuntime")


        self.encoder_session = self.__load_model(parse_path(encoder_model_path))
        self.encoder_input_name = self.encoder_session.get_inputs()[0].name
        self.decoder_session = self.__load_model(parse_path(decoder_model_path))

    def get_input_points(self, prompt):
        """Get input points"""
        points = []
        labels = []
        for mark in prompt:
            if mark["type"] == "point":
                points.append(mark["data"])
                labels.append(mark["label"])
            elif mark["type"] == "rectangle":
                points.append([mark["data"][0], mark["data"][1]])  # top left
                points.append(
                    [mark["data"][2], mark["data"][3]]
                )  # bottom right
                labels.append(2)
                labels.append(3)
        points, labels = np.array(points), np.array(labels)
        return points, labels

    def run_encoder(self, encoder_inputs):
        """Run encoder"""
        output = self.encoder_session.run(None, encoder_inputs)
        image_embedding = output[0]
        return image_embedding

    @staticmethod
    def get_preprocess_shape(oldh: int, oldw: int, long_side_length: int):
        """
        Compute the output size given input size and target long side length.
        """
        scale = long_side_length * 1.0 / max(oldh, oldw)
        newh, neww = oldh * scale, oldw * scale
        neww = int(neww + 0.5)
        newh = int(newh + 0.5)
        return (newh, neww)

    def apply_coords(self, coords: np.ndarray, original_size, target_length):
        """
        Expects a numpy array of length 2 in the final dimension. Requires the
        original image size in (H, W) format.
        """
        old_h, old_w = original_size
        new_h, new_w = self.get_preprocess_shape(
            original_size[0], original_size[1], target_length
        )
        coords = deepcopy(coords).astype(float)
        coords[..., 0] = coords[..., 0] * (new_w / old_w)
        coords[..., 1] = coords[..., 1] * (new_h / old_h)
        return coords

    def run_decoder(
        self, image_embedding, original_size, transform_matrix, prompt
    ):
        """Run decoder"""
        input_points, input_labels = self.get_input_points(prompt)

        # Add a batch index, concatenate a padding point, and transform.
        onnx_coord = np.concatenate(
            [input_points, np.array([[0.0, 0.0]])], axis=0
        )[None, :, :]
        onnx_label = np.concatenate([input_labels, np.array([-1])], axis=0)[
            None, :
        ].astype(np.float32)
        onnx_coord = self.apply_coords(
            onnx_coord, self.input_size, self.target_size
        ).astype(np.float32)

        # Apply the transformation matrix to the coordinates.
        onnx_coord = np.concatenate(
            [
                onnx_coord,
                np.ones((1, onnx_coord.shape[1], 1), dtype=np.float32),
            ],
            axis=2,
        )
        onnx_coord = np.matmul(onnx_coord, transform_matrix.T)
        onnx_coord = onnx_coord[:, :, :2].astype(np.float32)

        # Create an empty mask input and an indicator for no mask.
        onnx_mask_input = np.zeros((1, 1, 256, 256), dtype=np.float32)
        onnx_has_mask_input = np.zeros(1, dtype=np.float32)

        decoder_inputs = {
            "image_embeddings": image_embedding,
            "point_coords": onnx_coord,
            "point_labels": onnx_label,
            "mask_input": onnx_mask_input,
            "has_mask_input": onnx_has_mask_input,
            "orig_im_size": np.array(self.input_size, dtype=np.float32),
        }
        masks, _, _ = self.decoder_session.run(None, decoder_inputs)

        # Transform the masks back to the original image size.
        inv_transform_matrix = np.linalg.inv(transform_matrix)
        transformed_masks = self.transform_masks(
            masks, original_size, inv_transform_matrix
        )

        return transformed_masks



    def transform_masks(self, masks, original_size, transform_matrix):
        """Transform masks
        Transform the masks back to the original image size.
        """
        output_masks = []
        for batch in range(masks.shape[0]):
            batch_masks = []
            for mask_id in range(masks.shape[1]):
                mask = masks[batch, mask_id]
                mask = cv2.warpAffine(
                    mask,
                    transform_matrix[:2],
                    (original_size[1], original_size[0]),
                    flags=cv2.INTER_LINEAR,
                )
                batch_masks.append(mask)
            output_masks.append(batch_masks)
        return np.array(output_masks)

    def encode(self, cv_image):
        """
        Calculate embedding and metadata for a single image.
        """
        original_size = cv_image.shape[:2]

        # Calculate a transformation matrix to convert to self.input_size
        scale_x = self.input_size[1] / cv_image.shape[1]
        scale_y = self.input_size[0] / cv_image.shape[0]
        scale = min(scale_x, scale_y)
        transform_matrix = np.array(
            [
                [scale, 0, 0],
                [0, scale, 0],
                [0, 0, 1],
            ]
        )
        cv_image = cv2.warpAffine(
            cv_image,
            transform_matrix[:2],
            (self.input_size[1], self.input_size[0]),
            flags=cv2.INTER_LINEAR,
        )

        encoder_inputs = {
            self.encoder_input_name: cv_image.astype(np.float32),
        }
        image_embedding = self.run_encoder(encoder_inputs)
        return {
            "image_embedding": image_embedding,
            "original_size": original_size,
            "transform_matrix": transform_matrix,
        }

    def predict_masks(self, embedding, prompt=None):
        """
        Predict masks for a single image.
        """
        masks = self.run_decoder(
            embedding["image_embedding"],
            embedding["original_size"],
            embedding["transform_matrix"],
            prompt,
        )

        return masks
