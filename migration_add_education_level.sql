-- Migration: Add education_level field to users table
-- This migration adds the education_level field to support High School/College differentiation

-- Add education_level column to users table
ALTER TABLE `users` 
ADD COLUMN `education_level` ENUM('highschool', 'college') NULL AFTER `role`;

-- Update existing users to have a default education level (you may want to set this manually)
-- UPDATE `users` SET `education_level` = 'college' WHERE `education_level` IS NULL;

-- Add index for better query performance
CREATE INDEX `idx_users_education_level` ON `users` (`education_level`);

-- Add comment to document the change
ALTER TABLE `users` MODIFY COLUMN `education_level` ENUM('highschool', 'college') NULL COMMENT 'Education level: highschool or college';
