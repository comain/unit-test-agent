package com.example.mapper;

import org.apache.ibatis.annotations.Select;

public interface SampleMapper {
    @Select("SELECT name FROM sample WHERE id = #{id}")
    String selectById(String id);
}
